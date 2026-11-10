#!/usr/bin/env python3
"""
K8s AI Agent - Groq powered
Pure Python (kubernetes client + dulwich)
Supports GitHub / GitLab / Bitbucket tokens
No subprocess, no git binary needed
"""

import os
import json
import time
import tempfile
import shutil
from typing import Dict, Any, Optional
from pathlib import Path

from groq import Groq
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

# Load in-cluster config (when running as Pod)
try:
    config.load_incluster_config()
except config.ConfigException:
    config.load_kube_config()  # fallback for local testing

# Kubernetes clients
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
   - Example: detect StorageClass before creating StatefulSet with volumeClaimTemplates
3. Decide intelligently whether a Secret is needed:
   - Databases (Postgres, MySQL, MongoDB, Redis with auth) → YES
   - Kafka, Zookeeper, Nginx, most stateless apps → usually NO
4. Always wait until resources become Ready.
5. When asked to commit, push the generated YAML files to the Git repository mentioned in the prompt.
6. Prefer clean, production-ready YAML.

Available tools are provided. Use them step by step.
Never invent resource names or namespaces that the user did not request.
"""

# -------------------------------------------------
# Tool Definitions (for Groq)
# -------------------------------------------------
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "detect_storage_class",
            "description": "Detect available StorageClasses and return the default or best one",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
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
                    "yaml_content": {
                        "type": "string",
                        "description": "Full YAML content (can contain multiple documents separated by ---)"
                    },
                    "namespace": {
                        "type": "string",
                        "description": "Target namespace (optional, overrides metadata.namespace)"
                    }
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
                    "kind": {"type": "string", "description": "Resource kind (StatefulSet, Deployment, Pod, etc.)"},
                    "name": {"type": "string"},
                    "namespace": {"type": "string"},
                    "timeout_seconds": {"type": "integer", "default": 300}
                },
                "required": ["kind", "name", "namespace"]
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
            "name": "generate_password",
            "description": "Generate a secure random password",
            "parameters": {
                "type": "object",
                "properties": {
                    "length": {"type": "integer", "default": 32}
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "git_commit_and_push",
            "description": "Commit files and push to GitHub / GitLab / Bitbucket. Pass the exact repo_url from the user prompt.",
            "parameters": {
                "type": "object",
                "properties": {
                    "repo_url": {
                        "type": "string",
                        "description": "Full repository URL (https://github.com/... or https://gitlab.com/... or https://bitbucket.org/...)"
                    },
                    "branch": {"type": "string", "description": "Target branch"},
                    "files": {
                        "type": "object",
                        "description": "Dictionary of filename → file content (YAML)",
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
            all_scs.append({
                "name": name,
                "is_default": is_default,
                "provisioner": sc.provisioner
            })
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
    """Apply YAML using pure kubernetes Python client."""
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
                results.append({"error": "Missing kind or name in manifest"})
                continue

            try:
                resource = dyn_client.resources.get(api_version=api_version, kind=kind)
            except ResourceNotFoundError:
                return {"success": False, "error": f"Resource not found: {api_version}/{kind}"}

            try:
                # Try update first
                resource.get(name=name, namespace=ns if resource.namespaced else None)
                resource.patch(
                    body=doc,
                    name=name,
                    namespace=ns if resource.namespaced else None,
                    content_type="application/merge-patch+json"
                )
                action = "configured"
            except ApiException as e:
                if e.status == 404:
                    resource.create(
                        body=doc,
                        namespace=ns if resource.namespaced else None
                    )
                    action = "created"
                else:
                    raise

            results.append({
                "action": action,
                "kind": kind,
                "name": name,
                "namespace": ns if resource.namespaced else None
            })

        return {"success": True, "results": results}

    except Exception as e:
        return {"success": False, "error": str(e)}


def k8s_wait_ready(kind: str, name: str, namespace: str, timeout_seconds: int = 300) -> Dict[str, Any]:
    kind_lower = kind.lower()
    start = time.time()

    while time.time() - start < timeout_seconds:
        try:
            if kind_lower in ["statefulset", "sts"]:
                obj = apps_v1.read_namespaced_stateful_set(name, namespace)
                ready = obj.status.ready_replicas or 0
                desired = obj.spec.replicas or 0
                if ready == desired and desired > 0:
                    return {"ready": True, "ready_replicas": ready, "desired": desired}

            elif kind_lower in ["deployment", "deploy"]:
                obj = apps_v1.read_namespaced_deployment(name, namespace)
                ready = obj.status.ready_replicas or 0
                desired = obj.spec.replicas or 0
                if ready == desired and desired > 0:
                    return {"ready": True, "ready_replicas": ready, "desired": desired}

            elif kind_lower == "pod":
                obj = core_v1.read_namespaced_pod(name, namespace)
                if obj.status.phase == "Running":
                    if obj.status.container_statuses:
                        if all(c.ready for c in obj.status.container_statuses):
                            return {"ready": True, "phase": "Running"}
                    else:
                        return {"ready": True, "phase": "Running"}

            else:
                return {"error": f"Unsupported kind for wait: {kind}"}

        except ApiException as e:
            if e.status != 404:
                return {"error": str(e)}

        time.sleep(5)

    return {"ready": False, "error": f"Timeout after {timeout_seconds}s"}


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

        return {
            "success": True,
            "object": api_client.sanitize_for_serialization(obj)
        }
    except ApiException as e:
        return {"success": False, "error": str(e)}


def generate_password(length: int = 32) -> Dict[str, Any]:
    import secrets
    import string
    alphabet = string.ascii_letters + string.digits
    password = ''.join(secrets.choice(alphabet) for _ in range(length))
    return {"password": password}


def git_commit_and_push(
    repo_url: str,
    branch: str,
    files: Dict[str, str],
    commit_message: str
) -> Dict[str, Any]:
    """
    Pure Python Git using dulwich (no git binary required).
    Automatically selects the correct token based on the repo_url domain.
    """
    repo_url = repo_url.strip()
    if not repo_url.endswith(".git"):
        repo_url += ".git"

    # Detect provider + inject token
    if "github.com" in repo_url:
        token = GITHUB_TOKEN
        provider = "github"
        auth_url = repo_url.replace("https://", f"https://x-access-token:{token}@")
    elif "gitlab.com" in repo_url or "gitlab." in repo_url:
        token = GITLAB_TOKEN
        provider = "gitlab"
        auth_url = repo_url.replace("https://", f"https://oauth2:{token}@")
    elif "bitbucket.org" in repo_url:
        token = BITBUCKET_TOKEN
        provider = "bitbucket"
        auth_url = repo_url.replace("https://", f"https://x-token-auth:{token}@")
    else:
        return {"success": False, "error": f"Unsupported Git provider in URL: {repo_url}"}

    if not token:
        return {"success": False, "error": f"{provider.upper()}_TOKEN environment variable is not set"}

    tmpdir = None
    try:
        tmpdir = tempfile.mkdtemp(prefix="ai-agent-git-")

        # Clone
        try:
            porcelain.clone(auth_url, tmpdir, branch=branch.encode())
        except Exception:
            # Branch does not exist → clone default branch
            porcelain.clone(auth_url, tmpdir)

        repo = DulwichRepo(tmpdir)

        # Ensure target branch exists
        branch_ref = f"refs/heads/{branch}".encode()
        try:
            repo.refs[branch_ref]
        except KeyError:
            repo.refs[branch_ref] = repo.head()

        # Point HEAD to the branch
        porcelain.update_head(repo, branch.encode())

        # Write files into working tree
        for filename, content in files.items():
            file_path = Path(tmpdir) / filename
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(content, encoding="utf-8")

        # Stage everything
        porcelain.add(repo.path)

        # Commit
        author = b"K8s AI Agent <ai-agent@k8s.local>"
        porcelain.commit(
            repo.path,
            message=commit_message.encode(),
            author=author,
            committer=author
        )

        # Push
        porcelain.push(
            repo.path,
            auth_url,
            branch_ref
        )

        return {
            "success": True,
            "provider": provider,
            "branch": branch,
            "message": "Successfully committed and pushed (dulwich)"
        }

    except Exception as e:
        return {"success": False, "error": str(e)}
    finally:
        if tmpdir and os.path.exists(tmpdir):
            shutil.rmtree(tmpdir, ignore_errors=True)


# -------------------------------------------------
# Tool Router
# -------------------------------------------------
def execute_tool(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    if name == "detect_storage_class":
        return detect_storage_class()
    elif name == "k8s_apply":
        return k8s_apply(**args)
    elif name == "k8s_wait_ready":
        return k8s_wait_ready(**args)
    elif name == "k8s_get":
        return k8s_get(**args)
    elif name == "generate_password":
        return generate_password(**args)
    elif name == "git_commit_and_push":
        return git_commit_and_push(**args)
    else:
        return {"error": f"Unknown tool: {name}"}


# -------------------------------------------------
# Main Agent Loop
# -------------------------------------------------
def run_agent(user_prompt: str) -> str:
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt}
    ]

    print(f"\n[Agent] Starting with prompt:\n{user_prompt}\n")

    max_iterations = 25
    for _ in range(max_iterations):
        response = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=messages,
            tools=TOOLS,
            tool_choice="auto",
            temperature=0.1,
            max_tokens=4096
        )

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

            print(f"\n[Tool Call] {fn_name}")
            print(json.dumps(fn_args, indent=2))

            result = execute_tool(fn_name, fn_args)

            print(f"[Tool Result] {json.dumps(result, indent=2)[:1200]}")
            print("-" * 60)

            messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "name": fn_name,
                "content": json.dumps(result)
            })

    return "Agent reached maximum iterations."


# -------------------------------------------------
# Entry point
# -------------------------------------------------
if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        prompt = " ".join(sys.argv[1:])
    else:
        prompt = """
        Deploy PostgreSQL 16 StatefulSet with 3 replicas in middleware namespace.
        Store the password in a Secret.
        Detect StorageClass and configure 20Gi storage.
        Validate pod health and wait until Ready.
        Finally commit the generated YAMLs to https://github.com/ashishkumar256/ai-agent in the manifest branch.
        """

    final_answer = run_agent(prompt)
    print("\n========== FINAL ANSWER ==========")
    print(final_answer)