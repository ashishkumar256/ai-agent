#!/usr/bin/env python3
"""
K8s AI Agent - Groq powered
Pure Python (kubernetes client + dulwich)
Supports GitHub / GitLab / Bitbucket tokens
Interactive input via kubectl attach (type /end to finish prompt)
"""

import os
import sys
import json
import re
import time
import tempfile
import shutil
from typing import Dict, Any, Optional
from pathlib import Path

from groq import BadRequestError, Groq, RateLimitError
from kubernetes import client, config, dynamic
from kubernetes.client.rest import ApiException
from kubernetes.dynamic.exceptions import ResourceNotFoundError
import yaml

from dulwich import porcelain
from dulwich.repo import Repo as DulwichRepo

# -------------------------------------------------
# Configuration
# -------------------------------------------------
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GITLAB_TOKEN = os.getenv("GITLAB_TOKEN")
BITBUCKET_TOKEN = os.getenv("BITBUCKET_TOKEN")

if not GROQ_API_KEY:
    raise RuntimeError("GROQ_API_KEY environment variable is required")

# Recommended models (as of Nov 2026)
# Primary: openai/gpt-oss-120b   (strong, tool-calling)
# Alternatives:
#   openai/gpt-oss-20b
#   qwen/qwen3.8-27b
#   meta-llama/llama-4-scout-17b-16e-instruct
MODEL_NAME = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")

# Load in-cluster config
try:
    config.load_incluster_config()
except config.ConfigException:
    config.load_kube_config()

api_client = client.ApiClient()
core_v1 = client.CoreV1Api()
apps_v1 = client.AppsV1Api()
storage_v1 = client.StorageV1Api()
dyn_client = dynamic.DynamicClient(api_client)

groq_client = Groq(api_key=GROQ_API_KEY)

# -------------------------------------------------
# System Prompt
# -------------------------------------------------
SYSTEM_PROMPT = """
You are an expert Kubernetes SRE AI Agent.

Your responsibilities:
1. Understand the user's natural language request.
2. Create ALL required Kubernetes objects in the correct dependency order.
   - Example: Secret → Service → StatefulSet
   - Always detect StorageClass before creating StatefulSet with volumeClaimTemplates
3. Decide intelligently whether a Secret is needed:
   - Databases (Postgres, MySQL, MongoDB, Redis with auth) → YES
   - Kafka, Zookeeper, Nginx, most stateless apps → usually NO
4. Always wait until resources become Ready.
    - k8s_wait_ready waits no longer than 60 seconds and may return earlier when ready.
    - If readiness times out, inspect k8s_events for the affected resource first.
    - Only if no relevant events are found, inspect k8s_logs for the affected pod(s).
    - For k8s_logs, set tail_lines to 200; the tool returns only filtered failure-related lines.
    - Keep troubleshooting evidence concise; do not request full event histories or logs.
5. When asked to commit, push the generated YAML files to the Git repository mentioned in the prompt.
6. Prefer clean, production-ready YAML.

Available tools are provided. Use them step by step.
Never invent resource names or namespaces that the user did not request.
"""

# -------------------------------------------------
# Tool Definitions
# -------------------------------------------------
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "detect_storage_class",
            "description": "Detect available StorageClasses and return the default or best one",
            "parameters": {"type": "object", "properties": {}, "required": []}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "k8s_apply",
            "description": "Apply one or more Kubernetes YAML manifests (supports multi-document YAML)",
            "parameters": {
                "type": "object",
                "properties": {
                    "yaml_content": {"type": "string", "description": "Full YAML content"},
                    "namespace": {"type": ["string", "null"], "description": "Target namespace (optional)"}
                },
                "required": ["yaml_content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "k8s_wait_ready",
            "description": "Wait until a resource becomes Ready",
            "parameters": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string"},
                    "name": {"type": "string"},
                    "namespace": {"type": "string"},
                    "timeout_seconds": {"type": "integer", "maximum": 60}
                },
                "required": ["kind", "name", "namespace", "timeout_seconds"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "k8s_get",
            "description": "Get details of a Kubernetes resource",
            "parameters": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string"},
                    "name": {"type": "string"},
                    "namespace": {"type": "string"}
                },
                "required": ["kind", "name", "namespace"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "k8s_events",
            "description": "Get a compact summary of recent events in a namespace, optionally filtered to a resource",
            "parameters": {
                "type": "object",
                "properties": {
                    "namespace": {"type": "string"},
                    "name": {"type": "string"},
                    "kind": {"type": "string"}
                },
                "required": ["namespace"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "k8s_logs",
            "description": "Scan up to tail_lines (maximum 200) recent pod log lines and return only error/failure/status-code matches; call after checking events",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "namespace": {"type": "string"},
                    "container": {"type": "string"},
                    "tail_lines": {"type": "integer", "maximum": 200}
                },
                "required": ["name", "namespace", "tail_lines"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "k8s_delete",
            "description": "Delete one namespaced Kubernetes resource after user confirmation",
            "parameters": {
                "type": "object",
                "properties": {
                    "api_version": {"type": "string"},
                    "kind": {"type": "string"},
                    "name": {"type": "string"},
                    "namespace": {"type": "string"}
                },
                "required": ["api_version", "kind", "name", "namespace"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "generate_password",
            "description": "Generate a secure random password",
            "parameters": {
                "type": "object",
                "properties": {"length": {"type": "integer", "default": 32}},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "git_commit_and_push",
            "description": "Commit files and push to GitHub/GitLab/Bitbucket. Pass the exact repo_url from the user prompt.",
            "parameters": {
                "type": "object",
                "properties": {
                    "repo_url": {"type": "string"},
                    "branch": {"type": "string"},
                    "files": {
                        "type": "object",
                        "description": "filename → content",
                        "additionalProperties": {"type": "string"}
                    },
                    "commit_message": {"type": "string"}
                },
                "required": ["repo_url", "branch", "files", "commit_message"]
            }
        }
    }
]

# -------------------------------------------------
# Tool Implementations
# -------------------------------------------------
def detect_storage_class() -> Dict[str, Any]:
    try:
        sc_list = storage_v1.list_storage_class()
        default_sc = None
        all_scs = []
        for sc in sc_list.items:
            name = sc.metadata.name
            is_default = (
                sc.metadata.annotations
                and sc.metadata.annotations.get("storageclass.kubernetes.io/is-default-class") == "true"
            )
            all_scs.append({"name": name, "is_default": is_default, "provisioner": sc.provisioner})
            if is_default:
                default_sc = name
        return {
            "default_storage_class": default_sc,
            "available_storage_classes": all_scs,
            "recommended": default_sc or (all_scs[0]["name"] if all_scs else None)
        }
    except Exception as e:
        return {"error": str(e)}


def k8s_apply(yaml_content: str, namespace: Optional[str] = None) -> Dict[str, Any]:
    try:
        docs = list(yaml.safe_load_all(yaml_content))
        results = []
        for doc in docs:
            if not doc or not isinstance(doc, dict):
                continue
            if namespace:
                doc.setdefault("metadata", {})["namespace"] = namespace

            api_version = doc.get("apiVersion", "v1")
            kind = doc.get("kind")
            metadata = doc.get("metadata", {})
            name = metadata.get("name")
            ns = metadata.get("namespace", "default")

            if not kind or not name:
                results.append({"error": "Missing kind or name"})
                continue

            try:
                resource = dyn_client.resources.get(api_version=api_version, kind=kind)
            except ResourceNotFoundError:
                return {"success": False, "error": f"Resource not found: {api_version}/{kind}"}

            try:
                resource.get(name=name, namespace=ns if resource.namespaced else None)
                resource.patch(
                    body=doc, name=name,
                    namespace=ns if resource.namespaced else None,
                    content_type="application/merge-patch+json"
                )
                action = "configured"
            except ApiException as e:
                if e.status == 404:
                    resource.create(body=doc, namespace=ns if resource.namespaced else None)
                    action = "created"
                else:
                    raise

            results.append({"action": action, "kind": kind, "name": name, "namespace": ns if resource.namespaced else None})
        return {"success": True, "results": results}
    except Exception as e:
        return {"success": False, "error": str(e)}


def k8s_wait_ready(kind: str, name: str, namespace: str, timeout_seconds: int = 60) -> Dict[str, Any]:
    kind_lower = kind.lower()
    timeout_seconds = max(1, min(timeout_seconds, 60))
    start = time.time()
    while True:
        remaining = timeout_seconds - (time.time() - start)
        if remaining <= 0:
            return {
                "ready": False,
                "timed_out": True,
                "kind": kind,
                "name": name,
                "namespace": namespace,
                "timeout_seconds": timeout_seconds,
                "next_step": "Check k8s_events for this resource; if no relevant events exist, check k8s_logs for its pod."
            }
        try:
            if kind_lower in ["statefulset", "sts"]:
                obj = apps_v1.read_namespaced_stateful_set(name, namespace, _request_timeout=remaining)
                ready = obj.status.ready_replicas or 0
                desired = obj.spec.replicas or 0
                if ready == desired:
                    return {"ready": True, "ready_replicas": ready, "desired": desired}
            elif kind_lower in ["deployment", "deploy"]:
                obj = apps_v1.read_namespaced_deployment(name, namespace, _request_timeout=remaining)
                ready = obj.status.ready_replicas or 0
                desired = obj.spec.replicas or 0
                if ready == desired:
                    return {"ready": True, "ready_replicas": ready, "desired": desired}
            elif kind_lower == "pod":
                obj = core_v1.read_namespaced_pod(name, namespace, _request_timeout=remaining)
                statuses = obj.status.container_statuses or []
                if obj.status.phase == "Running" and statuses and all(container.ready for container in statuses):
                    return {"ready": True, "phase": "Running", "containers_ready": len(statuses)}
            else:
                return {"error": f"Unsupported kind for wait: {kind}"}
        except ApiException as e:
            if e.status != 404:
                return {"ready": False, "error": str(e)[:500]}

        remaining = timeout_seconds - (time.time() - start)
        if remaining <= 0:
            return {
                "ready": False,
                "timed_out": True,
                "kind": kind,
                "name": name,
                "namespace": namespace,
                "timeout_seconds": timeout_seconds,
                "next_step": "Check k8s_events for this resource; if no relevant events exist, check k8s_logs for its pod."
            }
        time.sleep(min(5, remaining))


def k8s_get(kind: str, name: str, namespace: str) -> Dict[str, Any]:
    try:
        kind_lower = kind.lower()
        if kind_lower in ["statefulset", "sts"]:
            obj = apps_v1.read_namespaced_stateful_set(name, namespace)
        elif kind_lower in ["deployment", "deploy"]:
            obj = apps_v1.read_namespaced_deployment(name, namespace)
        elif kind_lower in ["service", "svc"]:
            obj = core_v1.read_namespaced_service(name, namespace)
        elif kind_lower == "secret":
            obj = core_v1.read_namespaced_secret(name, namespace)
        elif kind_lower == "pod":
            obj = core_v1.read_namespaced_pod(name, namespace)
        elif kind_lower == "configmap":
            obj = core_v1.read_namespaced_config_map(name, namespace)
        else:
            resource = dyn_client.resources.get(kind=kind)
            obj = resource.get(name=name, namespace=namespace)
            return {"success": True, "object": obj.to_dict()}
        return {"success": True, "object": api_client.sanitize_for_serialization(obj)}
    except ApiException as e:
        return {"success": False, "error": str(e)}


def k8s_events(namespace: str, name: Optional[str] = None, kind: Optional[str] = None) -> Dict[str, Any]:
    try:
        selectors = []
        if name:
            selectors.append(f"involvedObject.name={name}")
        if kind:
            selectors.append(f"involvedObject.kind={kind}")
        response = core_v1.list_namespaced_event(
            namespace,
            field_selector=",".join(selectors) or None,
            limit=100
        )
        items = sorted(
            response.items,
            key=lambda event: (
                event.last_timestamp or event.event_time or event.first_timestamp
            ).timestamp() if (event.last_timestamp or event.event_time or event.first_timestamp) else 0,
            reverse=True
        )
        compact_events = []
        for event in items[:5]:
            compact_events.append({
                "type": event.type,
                "reason": event.reason,
                "object": f"{event.involved_object.kind}/{event.involved_object.name}",
                "count": event.count,
                "message": (event.message or "")[:300]
            })
        return {"found": bool(compact_events), "count": len(items), "events": compact_events}
    except ApiException as e:
        return {"found": False, "error": str(e)[:400]}


def k8s_logs(name: str, namespace: str, container: Optional[str] = None, tail_lines: int = 40) -> Dict[str, Any]:
    try:
        tail_lines = max(1, min(tail_lines, 200))
        log_text = core_v1.read_namespaced_pod_log(
            name,
            namespace,
            container=container,
            tail_lines=tail_lines,
            timestamps=True
        )
        lines = log_text.splitlines()
        failure_pattern = re.compile(
            r"error|fatal|panic|crit(?:ical)?|exception|stack[\s_-]*trace|traceback|"
            r"oomkilled|\bkilled\b|fail(?:ed|ure)?|refused|tim(?:e|ed)[\s_-]*out|unhealthy|"
            r"missing|not found|permission denied|crashloopbackoff|imagepullbackoff|"
            r"errimagepull|back-off|connection reset|http\s*/?\s*[45]\d{2}|"
            r"status(?:\s*code)?\s*[:=]?\s*[45]\d{2}|\b[45]\d{2}\b",
            re.IGNORECASE
        )
        matching_lines = [line[:400] for line in lines if failure_pattern.search(line)]
        selected_lines = matching_lines[-20:]
        filtered_logs = "\n".join(selected_lines)[-2500:]
        return {
            "pod": name,
            "namespace": namespace,
            "container": container,
            "scanned_lines": len(lines),
            "matched_lines": len(matching_lines),
            "returned_lines": len(selected_lines),
            "logs": filtered_logs,
            "message": "No failure-related log lines matched." if not matching_lines else None
        }
    except ApiException as e:
        return {"pod": name, "namespace": namespace, "error": str(e)[:400]}


def k8s_delete(api_version: str, kind: str, name: str, namespace: str) -> Dict[str, Any]:
    try:
        resource = dyn_client.resources.get(api_version=api_version, kind=kind)
        if not resource.namespaced:
            return {"success": False, "error": "Cluster-scoped resources cannot be deleted"}
        resource.delete(name=name, namespace=namespace)
        return {"success": True, "deleted": kind, "name": name, "namespace": namespace}
    except ApiException as e:
        return {"success": False, "error": str(e)}
    except ResourceNotFoundError:
        return {"success": False, "error": f"Resource not found: {api_version}/{kind}"}


def generate_password(length: int = 32) -> Dict[str, Any]:
    import secrets, string
    alphabet = string.ascii_letters + string.digits
    return {"password": ''.join(secrets.choice(alphabet) for _ in range(length))}


def git_commit_and_push(repo_url: str, branch: str, files: Dict[str, str], commit_message: str) -> Dict[str, Any]:
    repo_url = repo_url.strip()
    if not repo_url.endswith(".git"):
        repo_url += ".git"

    if "github.com" in repo_url:
        token, provider = GITHUB_TOKEN, "github"
        auth_url = repo_url.replace("https://", f"https://x-access-token:{token}@")
    elif "gitlab.com" in repo_url or "gitlab." in repo_url:
        token, provider = GITLAB_TOKEN, "gitlab"
        auth_url = repo_url.replace("https://", f"https://oauth2:{token}@")
    elif "bitbucket.org" in repo_url:
        token, provider = BITBUCKET_TOKEN, "bitbucket"
        auth_url = repo_url.replace("https://", f"https://x-token-auth:{token}@")
    else:
        return {"success": False, "error": f"Unsupported Git provider: {repo_url}"}

    if not token:
        return {"success": False, "error": f"{provider.upper()}_TOKEN is not set"}

    tmpdir = None
    try:
        tmpdir = tempfile.mkdtemp(prefix="ai-agent-git-")
        try:
            porcelain.clone(auth_url, tmpdir, branch=branch.encode())
        except Exception:
            porcelain.clone(auth_url, tmpdir)

        repo = DulwichRepo(tmpdir)
        branch_ref = f"refs/heads/{branch}".encode()
        try:
            repo.refs[branch_ref]
        except KeyError:
            repo.refs[branch_ref] = repo.head()
        porcelain.update_head(repo, branch.encode())

        for filename, content in files.items():
            path = Path(tmpdir) / filename
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

        porcelain.add(repo.path)
        author = b"K8s AI Agent <ai-agent@k8s.local>"
        porcelain.commit(repo.path, message=commit_message.encode(), author=author, committer=author)
        porcelain.push(repo.path, auth_url, branch_ref)

        return {"success": True, "provider": provider, "branch": branch, "message": "Pushed successfully"}
    except Exception as e:
        return {"success": False, "error": str(e)}
    finally:
        if tmpdir and os.path.exists(tmpdir):
            shutil.rmtree(tmpdir, ignore_errors=True)


def execute_tool(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    mapping = {
        "detect_storage_class": detect_storage_class,
        "k8s_apply": k8s_apply,
        "k8s_wait_ready": k8s_wait_ready,
        "k8s_get": k8s_get,
        "k8s_events": k8s_events,
        "k8s_logs": k8s_logs,
        "k8s_delete": k8s_delete,
        "generate_password": generate_password,
        "git_commit_and_push": git_commit_and_push,
    }
    fn = mapping.get(name)
    if not fn:
        return {"error": f"Unknown tool: {name}"}
    return fn(**args)


def confirm_tool_call(name: str, args: Dict[str, Any]) -> bool:
    if name == "k8s_apply":
        print("\nManifest preview (YAML):")
        print(args.get("yaml_content", "<missing yaml_content>"))
        if args.get("namespace"):
            print(f"Target namespace override: {args['namespace']}")
    else:
        print("\nOperation details:")
        print(json.dumps(args, indent=2))

    try:
        answer = input(f"Approve tool action '{name}'? [y/N]: ").strip().lower()
    except EOFError:
        print("No interactive input; operation denied.")
        return False
    return answer in {"y", "yes"}


# -------------------------------------------------
# Agent Loop
# -------------------------------------------------
def run_agent(user_prompt: str) -> str:
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt}
    ]
    print(f"\n[Agent] Received prompt ({len(user_prompt)} chars). Starting...\n")

    for _ in range(25):
        for retry in range(5):
            try:
                response = groq_client.chat.completions.create(
                    model=MODEL_NAME,
                    messages=messages,
                    tools=TOOLS,
                    tool_choice="auto",
                    temperature=0.1,
                    max_tokens=4096
                )
                break
            except RateLimitError as error:
                if retry == 4:
                    raise

                delay = None
                response_headers = getattr(getattr(error, "response", None), "headers", {})
                retry_after = response_headers.get("retry-after")
                if retry_after:
                    try:
                        delay = float(retry_after.rstrip("s"))
                    except ValueError:
                        pass

                if delay is None:
                    match = re.search(r"Please try again in ([\d.]+)s", str(error), re.IGNORECASE)
                    delay = float(match.group(1)) if match else min(2 ** retry, 30)

                delay = max(0, delay)
                print(f"[Rate limit] Retrying in {delay:.2f}s (attempt {retry + 2}/5).")
                time.sleep(delay)
            except BadRequestError as error:
                body = getattr(error, "body", None)
                error_details = body.get("error", {}) if isinstance(body, dict) else {}
                if error_details.get("code") != "output_parse_failed" or retry == 4:
                    raise

                messages.append({
                    "role": "user",
                    "content": (
                        "Your previous response could not be parsed as a valid response. "
                        "Retry using a valid tool call when an action is needed; otherwise "
                        "return a concise final answer. Do not claim an action was completed "
                        "unless you invoked the corresponding tool."
                    )
                })
                print(f"[Response format] Retrying with a format correction (attempt {retry + 2}/5).")
        msg = response.choices[0].message
        messages.append(msg)

        if not msg.tool_calls:
            print("\n[Agent] Finished.")
            return msg.content or "No response from model."

        for tool_call in msg.tool_calls:
            fn_name = tool_call.function.name
            try:
                fn_args = json.loads(tool_call.function.arguments)
            except json.JSONDecodeError:
                fn_args = {}
            print(f"\n[Tool] {fn_name}")
            if confirm_tool_call(fn_name, fn_args):
                result = execute_tool(fn_name, fn_args)
            else:
                result = {"success": False, "cancelled": True, "message": "User did not approve this operation."}
            print(f"[Result] {json.dumps(result, indent=2)[:1000]}")
            print("-" * 50)
            messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "name": fn_name,
                "content": json.dumps(result)
            })
    return "Agent reached maximum iterations."


# -------------------------------------------------
# Interactive input (for kubectl attach)
# -------------------------------------------------
def read_prompt_until_end() -> str:
    print("=" * 60)
    print("K8s AI Agent ready.")
    print("Paste / type your prompt below.")
    print("When finished, type   /end   on a new line and press Enter.")
    print("=" * 60)
    sys.stdout.flush()

    lines = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if line.strip() == "/end":
            break
        lines.append(line)
    return "\n".join(lines).strip()


# -------------------------------------------------
# Entry point
# -------------------------------------------------
if __name__ == "__main__":
    if not sys.stdin.isatty() and len(sys.argv) == 1:
        # Running under kubectl attach / pipe
        prompt = read_prompt_until_end()
    elif len(sys.argv) > 1:
        prompt = " ".join(sys.argv[1:])
    else:
        prompt = read_prompt_until_end()

    if not prompt:
        print("No prompt received. Exiting.")
        sys.exit(1)

    final = run_agent(prompt)
    print("\n" + "=" * 60)
    print("FINAL ANSWER")
    print("=" * 60)
    print(final)