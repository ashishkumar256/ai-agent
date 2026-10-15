"""
Generic Kubernetes AI agent: Groq (OpenAI-style tool calling) + the official `kubernetes` Python client.
No kubectl, no subprocess, no command-line arguments.

Just run:   python k8s_agent.py
Prompts: Groq key (if GROQ_API_KEY is not set), model, kube context, the task, approvals.

- Cluster access comes from ~/.kube/config (or $KUBECONFIG).
- The model writes manifests as files into ~/manifest (or $MANIFEST_DIR), then applies them (server-side apply).
- Every change (apply, secret, exec, delete) and every new namespace needs your approval.
- Built for Groq's per-minute limits: reads the rate-limit headers and waits when needed, retries on 429,
  trims old history, keeps tool output small, and offers one-call tools to save requests.

Setup:
    pip install groq "kubernetes>=29" pyyaml
    (optional) export GROQ_API_KEY="your-key"

Docker:     docker compose run --rm k8s-agent      (see docker-compose.yaml)
"""
import getpass
import json
import os
import re
import secrets
import shlex
import sys
import time
from pathlib import Path

import yaml
from groq import APIConnectionError, APIStatusError, BadRequestError, Groq, RateLimitError
from kubernetes import client, config, dynamic
from kubernetes.client.rest import ApiException
from kubernetes.stream import stream

# ---- tuning (see console.groq.com/settings/limits for YOUR limits) ----------------------------
DEFAULT_MODEL = "openai/gpt-oss-120b"       # fallback only; the script lists your account's models at start
PREFERRED = ["openai/gpt-oss-120b", "llama-3.3-70b-versatile", "openai/gpt-oss-20b", "qwen/qwen3-32b",
             "moonshotai/kimi-k2-instruct", "llama-3.1-8b-instant"]     # first one available becomes the default
SKIP_WORDS = ("whisper", "tts", "guard", "playai", "orpheus", "embed")  # not chat/tool-calling models
MAX_OUT = 1500            # max completion tokens per call (completion tokens count toward tokens/minute)
MAX_RPM = 25              # stay under your requests-per-minute limit (free tier is usually 30)
KEEP_FULL = 8             # newest messages kept in full; older tool output / manifests are trimmed
MAX_TOOL_OUTPUT = 2500    # max characters of any tool result sent to the model
MAX_STEPS = 60
# -----------------------------------------------------------------------------------------------

# Where manifests are written. Default ~/manifest; override with $MANIFEST_DIR (the Docker image sets /manifest).
MANIFEST_DIR = Path(os.environ.get("MANIFEST_DIR") or Path.home() / "manifest").expanduser()
FILENAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\.ya?ml")

MUTATING = {"apply_manifest", "generate_secret", "pod_exec", "k8s_delete"}
ALWAYS_ASK = {"k8s_delete"}         # never offered the "always" shortcut
approved_always = set()
APPROVED_NS = []                    # namespaces the user approved this run (first = default)

core = None                         # CoreV1Api
dyn = None                          # DynamicClient (any kind / CRD)
llm = None                          # Groq client
MODEL = DEFAULT_MODEL


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def cut(text, n=None):
    n = n or MAX_TOOL_OUTPUT
    text = str(text)
    return text if len(text) <= n else text[:n] + f"\n...[truncated {len(text) - n} chars]"


def as_bool(v):
    return v is True or str(v).lower() == "true"


def as_list(v, sep=None):
    """Models sometimes send arrays as strings; accept both."""
    if isinstance(v, list):
        return v
    if isinstance(v, str):
        try:
            j = json.loads(v)
            if isinstance(j, list):
                return j
        except Exception:
            pass
        return [x.strip() for x in v.split(sep)] if sep else shlex.split(v)
    return []


def flush_stdin():
    """Drop pending keystrokes (e.g. leftovers from a paste) so they can't answer an approval prompt."""
    try:
        import termios
        termios.tcflush(sys.stdin, termios.TCIFLUSH)
    except Exception:
        try:
            import msvcrt
            while msvcrt.kbhit():
                msvcrt.getch()
        except Exception:
            pass


def check_ns(ns):
    """Ask the user the first time a namespace is touched."""
    if ns in APPROVED_NS:
        return
    flush_stdin()
    ans = input(f"\n  [approval needed] Allow the agent to use namespace '{ns}'? [y/N]: ")
    if ans.strip().lower() != "y":
        raise PermissionError(f"user did not approve namespace '{ns}'")
    APPROVED_NS.append(ns)


def pick_ns(namespace):
    ns = namespace or (APPROVED_NS[0] if APPROVED_NS else "")
    if not ns:
        raise ValueError("namespace is required: use the one from the user's task, or ask the user")
    check_ns(ns)
    return ns


def ns_for(res, namespace):
    """Namespace to use for a resource type (None for cluster-scoped)."""
    return pick_ns(namespace) if res.namespaced else None


def manifest_path(filename):
    if not FILENAME_RE.fullmatch(filename or ""):
        raise ValueError("filename must look like 10-config.yaml (letters, digits, . _ -; .yaml/.yml)")
    return MANIFEST_DIR / filename


def check_docs(docs):
    for d in docs:
        if not isinstance(d, dict) or "apiVersion" not in d or "kind" not in d \
                or not (d.get("metadata") or {}).get("name"):
            raise ValueError("every YAML document needs apiVersion, kind and metadata.name")


def load_docs(path):
    docs = [d for d in yaml.safe_load_all(path.read_text()) if d]
    check_docs(docs)
    return docs


def slim_annotations(meta):
    ann = dict(meta.get("annotations") or {})
    ann.pop("kubectl.kubernetes.io/last-applied-configuration", None)
    return {k: str(v)[:120] for k, v in ann.items()}


# --------------------------------------------------------------------------
# Tools: manifest files on the host (~/manifest)
# --------------------------------------------------------------------------
def write_manifest(filename, content):
    path = manifest_path(filename)
    docs = [d for d in yaml.safe_load_all(content) if d]           # validates YAML syntax
    if not docs:
        return "Rejected: no YAML documents in content"
    check_docs(docs)
    MANIFEST_DIR.mkdir(parents=True, exist_ok=True)
    existed = path.exists()
    path.write_text(content if content.endswith("\n") else content + "\n")
    what = ", ".join(f"{d['kind']}/{d['metadata']['name']}" for d in docs)
    return f"{'Updated' if existed else 'Wrote'} {path} containing: {what}"


def read_manifest(filename):
    return cut(manifest_path(filename).read_text(), 6000)


# --------------------------------------------------------------------------
# Tools: cluster
# --------------------------------------------------------------------------
def apply_manifest(filename):
    """Server-side apply every document in ~/manifest/<filename>."""
    docs = load_docs(manifest_path(filename))
    file_ns = {d["metadata"]["name"] for d in docs if d["kind"] == "Namespace"}
    plan = []                                    # validate everything first, apply after
    for d in docs:
        res = dyn.resources.get(api_version=d["apiVersion"], kind=d["kind"])
        meta = d["metadata"]
        if res.namespaced:
            if not meta.get("namespace"):
                meta["namespace"] = next(iter(file_ns)) if len(file_ns) == 1 else pick_ns("")
            elif meta["namespace"] not in file_ns:
                check_ns(meta["namespace"])
            ns = meta["namespace"]
        else:
            if d["kind"] != "Namespace":
                raise PermissionError(f"cluster-scoped {d['kind']}/{meta['name']} is not allowed "
                                      f"(only Namespace objects)")
            ns = None
        plan.append((res, d, ns))

    done = []
    for res, d, ns in plan:
        name = d["metadata"]["name"]
        try:
            dyn.server_side_apply(res, body=d, name=name, namespace=ns,
                                  field_manager="k8s-ai-agent", force_conflicts=True)
            done.append(f"applied {d['kind']}/{name}")
            if d["kind"] == "Namespace" and name not in APPROVED_NS:
                APPROVED_NS.append(name)         # user approved this apply, which listed the namespace
        except ApiException as e:
            done.append(f"FAILED {d['kind']}/{name}: {e.status} {e.reason} {cut(e.body, 800)}")
            break
    return "\n".join(done)


def namespace_overview(namespace=""):
    """One call instead of many: workloads, pods, PVCs, services."""
    ns = pick_ns(namespace)
    try:
        core.read_namespace(ns)
    except ApiException as e:
        if e.status == 404:
            return f"namespace {ns} does not exist yet (create it with a Namespace manifest)"
        raise
    apps = client.AppsV1Api()
    lines = []
    for s in apps.list_namespaced_stateful_set(ns).items:
        lines.append(f"StatefulSet {s.metadata.name}: ready {s.status.ready_replicas or 0}/{s.spec.replicas}")
    for dep in apps.list_namespaced_deployment(ns).items:
        lines.append(f"Deployment {dep.metadata.name}: ready {dep.status.ready_replicas or 0}/{dep.spec.replicas}")
    for p in core.list_namespaced_pod(ns).items:
        cs = p.status.container_statuses or []
        ready = f"{sum(1 for c in cs if c.ready)}/{len(cs)}"
        restarts = sum(c.restart_count for c in cs)
        why = next((c.state.waiting.reason for c in cs if c.state and c.state.waiting), "")
        lines.append(f"Pod {p.metadata.name}: {p.status.phase} ready {ready} restarts {restarts} "
                     f"node {p.spec.node_name}" + (f" waiting={why}" if why else ""))
    for c in core.list_namespaced_persistent_volume_claim(ns).items:
        cap = (c.status.capacity or {}).get("storage", "-")
        lines.append(f"PVC {c.metadata.name}: {c.status.phase} {cap} storageClass={c.spec.storage_class_name}")
    for s in core.list_namespaced_service(ns).items:
        ports = ",".join(str(p.port) for p in s.spec.ports or [])
        lines.append(f"Service {s.metadata.name}: {s.spec.type} {s.spec.cluster_ip} ports {ports}")
    return cut("\n".join(lines) or f"namespace {ns} is empty")


def k8s_get(kind, api_version, namespace="", name="", label_selector="", path=""):
    res = dyn.resources.get(api_version=api_version, kind=kind)
    ns = ns_for(res, namespace)
    if name:
        obj = res.get(name=name, namespace=ns).to_dict()
        meta = obj.get("metadata", {})
        meta.pop("managedFields", None)
        if "annotations" in meta:
            meta["annotations"] = slim_annotations(meta)
        for key in [k for k in path.split(".") if k]:        # return only a sub-tree to save tokens
            try:
                obj = obj[int(key)] if isinstance(obj, list) else obj[key]
            except (KeyError, IndexError, ValueError, TypeError):
                return f"path '{path}' not found"
        return cut(json.dumps(obj, separators=(",", ":"), default=str))
    kw = {}
    if ns:
        kw["namespace"] = ns
    if label_selector:
        kw["label_selector"] = label_selector
    items = res.get(**kw).items
    if not items:
        return "No items found"
    lines = []
    for it in items:
        d = it.to_dict()
        m = d.get("metadata", {})
        ann = json.dumps(slim_annotations(m))[:160]
        st = json.dumps(d.get("status") or {}, default=str)[:250]
        lines.append(f"{m.get('name')} | ann={ann} | status={st}")
    return cut("\n".join(lines))


def k8s_events(namespace="", object_name=""):
    ns = pick_ns(namespace)
    kw = {"field_selector": f"involvedObject.name={object_name}"} if object_name else {}
    events = core.list_namespaced_event(ns, **kw).items
    events.sort(key=lambda e: e.last_timestamp or e.event_time or e.metadata.creation_timestamp)
    lines = [f"{e.type} {e.reason} {e.involved_object.name}: {(e.message or '')[:200]}"
             for e in events[-20:]]
    return cut("\n".join(lines) or "No events")


def pod_logs(pod, container="", namespace="", previous=False, tail=60):
    ns = pick_ns(namespace)
    kw = {"container": container} if container else {}
    return cut(core.read_namespaced_pod_log(pod, ns, tail_lines=int(tail),
                                            previous=as_bool(previous), **kw) or "(empty log)")


def _is_ready(kind, d):
    spec, st = d.get("spec") or {}, d.get("status") or {}
    if kind in ("StatefulSet", "Deployment"):
        want = spec.get("replicas", 1)
        ok = st.get("readyReplicas", 0) == want and st.get("updatedReplicas", 0) == want
        if kind == "Deployment":
            ok = ok and st.get("availableReplicas", 0) == want
        return ok
    if kind == "Pod":
        return any(c.get("type") == "Ready" and c.get("status") == "True"
                   for c in st.get("conditions", []))
    if kind == "Job":
        return st.get("succeeded", 0) >= 1
    raise ValueError("wait_until_ready supports StatefulSet, Deployment, Pod and Job")


BAD_WAITING = {"ImagePullBackOff", "ErrImagePull", "CrashLoopBackOff", "CreateContainerConfigError",
               "CreateContainerError", "InvalidImageName", "RunContainerError"}


def pod_problems(pods):
    """{'pod/container': 'Reason: message'} for pods that look stuck or failing."""
    found = {}
    for p in pods:
        st = p.status
        for cs in (st.init_container_statuses or []) + (st.container_statuses or []):
            w = cs.state.waiting if cs.state else None
            if w and w.reason in BAD_WAITING:
                found[f"{p.metadata.name}/{cs.name}"] = f"{w.reason}: {(w.message or '')[:250]}"
        for c in st.conditions or []:
            if c.type == "PodScheduled" and c.status == "False" and c.reason == "Unschedulable":
                found[f"{p.metadata.name}/scheduling"] = f"Unschedulable: {(c.message or '')[:250]}"
    return found


def wait_until_ready(kind, api_version, name, namespace="", timeout_seconds=240):
    """Poll (no LLM calls) until ready. Returns early with evidence if a pod is clearly stuck."""
    res = dyn.resources.get(api_version=api_version, kind=kind)
    ns = ns_for(res, namespace)
    start = time.time()
    deadline = start + min(int(timeout_seconds), 600)
    seen, status, last_print = {}, "", -10
    while True:
        d, pods = {}, []
        try:
            d = res.get(name=name, namespace=ns).to_dict()
            status = json.dumps(d.get("status"), default=str)[:500]
            if _is_ready(kind, d):
                return f"READY {kind}/{name}: {status}"
            if kind == "Pod":
                pods = [core.read_namespaced_pod(name, ns)]
            else:
                match = ((d.get("spec") or {}).get("selector") or {}).get("matchLabels") or {}
                if match:
                    sel = ",".join(f"{k}={v}" for k, v in match.items())
                    pods = core.list_namespaced_pod(ns, label_selector=sel).items
        except ApiException as e:
            status = f"{e.status} {e.reason}"

        problems = pod_problems(pods)
        seen = {k: seen.get(k, 0) + 1 for k in problems}            # consecutive polls only
        stuck = {k: v for k, v in problems.items()
                 if seen[k] >= (6 if k.endswith("/scheduling") else 3)}
        elapsed = int(time.time() - start)
        if stuck:
            first_pod = next(iter(stuck)).split("/")[0]
            return (f"PROBLEM: {kind}/{name} not ready after {elapsed}s.\n"
                    + "\n".join(f"- {k} -> {v}" for k, v in stuck.items())
                    + f"\nEvents for {first_pod}:\n{k8s_events(ns, first_pod)[:1000]}\n"
                    "Fix the root cause in the manifest and re-apply. A StatefulSet does not replace a "
                    "not-ready pod after a spec fix: k8s_delete the stuck pod (kind Pod, api_version v1).")
        if elapsed - last_print >= 10:                              # visible progress for the human
            last_print = elapsed
            phases = ", ".join(f"{p.metadata.name}={p.status.phase}" for p in pods) or "no pods yet"
            print(f"    ... waiting for {kind}/{name} ({elapsed}s): {phases}"
                  + (f" | suspect: {', '.join(problems)}" if problems else ""))
        if time.time() > deadline:
            return f"TIMEOUT waiting for {kind}/{name} after {elapsed}s: {status}"
        time.sleep(5)


def _exec_one(ns, pod, command, container, timeout):
    kw = {"container": container} if container else {}
    ws = stream(core.connect_get_namespaced_pod_exec, pod, ns, command=command,
                stderr=True, stdin=False, stdout=True, tty=False,
                _preload_content=False, **kw)
    out, deadline = [], time.time() + timeout
    while ws.is_open() and time.time() < deadline:
        ws.update(timeout=1)
        if ws.peek_stdout():
            out.append(ws.read_stdout())
        if ws.peek_stderr():
            out.append(ws.read_stderr())
    ws.close()
    return f"[{pod}] exit={ws.returncode}\n{''.join(out).strip()}"


def pod_exec(command, pod="", pods=None, namespace="", container="", timeout_seconds=60):
    """Run one command in one pod, or the same command in several pods (one request instead of many)."""
    ns = pick_ns(namespace)
    command = [str(c) for c in as_list(command)]
    targets = as_list(pods, ",") + ([pod] if pod else [])
    if not targets or not command:
        return "Error: give command and pod (or pods)"
    return cut("\n".join(_exec_one(ns, t, command, container, int(timeout_seconds)) for t in targets))


def generate_secret(name, keys, namespace=""):
    """Create a Secret with random values (values are never shown to the model)."""
    ns = pick_ns(namespace)
    keys = [str(k) for k in as_list(keys, ",")]
    try:
        core.read_namespaced_secret(name, ns)
        return f"secret {ns}/{name} already exists, left unchanged"
    except ApiException as e:
        if e.status != 404:
            raise
    body = client.V1Secret(metadata=client.V1ObjectMeta(name=name),
                           string_data={k: secrets.token_urlsafe(24) for k in keys})
    core.create_namespaced_secret(ns, body)
    return f"created secret {ns}/{name} with keys {keys} (values not shown)"


def k8s_delete(kind, api_version, name, namespace=""):
    res = dyn.resources.get(api_version=api_version, kind=kind)
    if not res.namespaced:
        return "Rejected: deleting cluster-scoped resources is not allowed"
    ns = ns_for(res, namespace)
    res.delete(name=name, namespace=ns)
    return f"deleted {kind}/{name} in namespace {ns}"


TOOLS = {f.__name__: f for f in (
    namespace_overview, k8s_get, k8s_events, pod_logs, write_manifest, read_manifest,
    apply_manifest, wait_until_ready, pod_exec, generate_secret, k8s_delete)}


# --------------------------------------------------------------------------
# Approval gate + tool runner
# --------------------------------------------------------------------------
def describe_action(name, args):
    try:
        if name == "apply_manifest":
            items = []
            for d in load_docs(manifest_path(args["filename"])):
                m = d["metadata"]
                items.append(f"{d['kind']}/{m['name']}" + (f" (ns {m['namespace']})" if m.get("namespace") else ""))
            return f"apply {args['filename']}: " + ", ".join(items)
        if name == "pod_exec":
            targets = as_list(args.get("pods"), ",") + ([args["pod"]] if args.get("pod") else [])
            return f"exec in {targets}: {as_list(args.get('command'))}"
    except Exception:
        pass
    return json.dumps(args)[:300]


def approve(name, args):
    if name not in MUTATING or name in approved_always:
        return True
    flush_stdin()
    print(f"\n  [approval needed] {name}: {describe_action(name, args)}")
    opts = "[y]es / [n]o" + ("" if name in ALWAYS_ASK else " / [a]lways for this tool in this run")
    ans = input(f"  Allow? {opts}: ").strip().lower()
    if ans == "a" and name not in ALWAYS_ASK:
        approved_always.add(name)
        return True
    return ans == "y"


def run_tool(name, args):
    func = TOOLS.get(name)
    if func is None:
        return f"Unknown tool: {name}"
    if not approve(name, args):
        return "DENIED by user. Do not retry this action; explain what you need instead."
    try:
        return func(**args)
    except ApiException as e:
        return f"API error {e.status} {e.reason}: {cut(e.body, 1000)}"
    except Exception as e:
        return f"Error: {type(e).__name__}: {e}"     # model sees it and can recover


# --------------------------------------------------------------------------
# Tool descriptions (what the model sees) - kept short because they are resent on every call
# --------------------------------------------------------------------------
def tool(name, desc, props=None, required=None):
    return {"type": "function", "function": {
        "name": name, "description": desc,
        "parameters": {"type": "object", "properties": props or {}, "required": required or []}}}


S = {"type": "string"}
INT = {"type": "integer"}
LIST = {"type": "array", "items": {"type": "string"}}
KIND = {"type": "string", "description": "e.g. StatefulSet"}
APIV = {"type": "string", "description": "e.g. apps/v1, v1, storage.k8s.io/v1"}
NSP = {"type": "string", "description": "namespace from the task"}

TOOL_SPECS = [
    tool("namespace_overview", "One-call summary of StatefulSets, Deployments, Pods, PVCs, Services in a namespace.",
         {"namespace": NSP}),
    tool("k8s_get", "Get one object (name given) or list a kind. 'path' (dotted, e.g. status or spec.template.spec) "
         "returns only that part.",
         {"kind": KIND, "api_version": APIV, "namespace": NSP, "name": S, "label_selector": S, "path": S},
         ["kind", "api_version"]),
    tool("k8s_events", "Recent events in a namespace, optionally for one object name.",
         {"namespace": NSP, "object_name": S}),
    tool("pod_logs", "Pod container logs (previous=true after a crash).",
         {"pod": S, "container": S, "namespace": NSP, "previous": {"type": "boolean"}, "tail": INT}, ["pod"]),
    tool("write_manifest", "Save YAML (may hold several --- documents) to ~/manifest/<filename>, e.g. 20-statefulset.yaml. "
         "No secret values.", {"filename": S, "content": S}, ["filename", "content"]),
    tool("read_manifest", "Read a saved manifest file.", {"filename": S}, ["filename"]),
    tool("apply_manifest", "Server-side apply a saved manifest file to the cluster.", {"filename": S}, ["filename"]),
    tool("wait_until_ready", "Wait for a StatefulSet/Deployment/Pod/Job to be ready; returns PROBLEM early if pods are stuck.",
         {"kind": KIND, "api_version": APIV, "name": S, "namespace": NSP, "timeout_seconds": INT},
         ["kind", "api_version", "name"]),
    tool("pod_exec", "Run a command (argv list, no shell) in one pod or in several (pods list). Returns exit code and output.",
         {"command": LIST, "pod": S, "pods": LIST, "namespace": NSP, "container": S}, ["command"]),
    tool("generate_secret", "Create a Secret with random values for the keys, if missing. Use via secretKeyRef.",
         {"name": S, "keys": LIST, "namespace": NSP}, ["name", "keys"]),
    tool("k8s_delete", "Delete one namespaced object (needs user approval).",
         {"kind": KIND, "api_version": APIV, "name": S, "namespace": NSP}, ["kind", "api_version", "name"]),
]

SYSTEM_PROMPT = f"""You are a Kubernetes operations agent. Manifests are saved in {MANIFEST_DIR}.
Namespace: use the one in the user's task and pass it explicitly; if missing, ask the user in plain text.
Workflow:
1. Inspect first (namespace_overview, k8s_get). Never assume cluster state.
2. If the namespace does not exist, create it. write_manifest (one file per component, numbered:
   00-namespace.yaml, 10-..., 30-...), then apply_manifest. Do not ask the user for permission to do what the
   task asks: the user approves every change at a prompt anyway.
   Passwords: generate_secret + secretKeyRef; never put secret values in manifests.
3. Then wait_until_ready. On PROBLEM/TIMEOUT: diagnose (k8s_events, pod_logs, k8s_get with path), fix the same
   file, re-apply. Max 3 fix cycles, then report.
   - A StatefulSet will not replace a not-ready pod after a spec fix: k8s_delete that pod (kind Pod, api_version v1).
   - Image errors: wrong tag -> fix it; registry unreachable -> stop and tell the user.
   - Use official images (e.g. postgres:16), not Bitnami.
4. Verify with evidence (namespace_overview, pod_exec with pods=[...]). Put independent tool calls in one turn.
5. If requirements are unclear or a prerequisite is missing, ask the user in plain text.
6. If the user denies an action, do not retry; explain what you need.
7. Final report, short: files written, PASS/FAIL per check with evidence, caveats.
Be concise between tool calls."""


# --------------------------------------------------------------------------
# Groq calls with rate-limit handling
# --------------------------------------------------------------------------
class LimitReached(Exception):
    pass


RL = {"tokens_left": None, "tokens_limit": None, "tokens_reset": 0.0, "req_left": None, "last_call": 0.0}
STATS = {"calls": 0, "tokens": 0}
COMPACT = {"keep": KEEP_FULL}


def parse_duration(s):
    """'2m59.56s' / '7.66s' / '120ms' -> seconds."""
    mult = {"ms": 0.001, "s": 1, "m": 60, "h": 3600}
    return sum(float(n) * mult[u] for n, u in re.findall(r"(\d+(?:\.\d+)?)(ms|h|m|s)", s or ""))


def estimate_tokens(messages):
    return int((len(json.dumps(messages)) + len(json.dumps(TOOL_SPECS))) / 3.2) + 300


def record_headers(h):
    def num(k):
        try:
            return int(h.get(k))
        except Exception:
            return None
    RL["tokens_left"] = num("x-ratelimit-remaining-tokens")        # tokens per minute
    RL["tokens_limit"] = num("x-ratelimit-limit-tokens")
    RL["tokens_reset"] = time.time() + parse_duration(h.get("x-ratelimit-reset-tokens"))
    RL["req_left"] = num("x-ratelimit-remaining-requests")         # requests per DAY on Groq


def wait_for_budget(messages):
    """Sleep just long enough that this request fits the tokens-per-minute budget and RPM spacing."""
    now = time.time()
    if now >= RL["tokens_reset"]:
        RL["tokens_left"] = None
    if RL["req_left"] == 0:
        raise LimitReached("Groq daily request limit is used up. Wait for the reset or use another model/key.")
    delay = RL["last_call"] + 60.0 / MAX_RPM - now
    need, left, limit = estimate_tokens(messages), RL["tokens_left"], RL["tokens_limit"]
    if left is not None and need > left and (limit is None or need < limit):
        refill = (need - left) * 60.0 / limit if limit else RL["tokens_reset"] - now
        delay = max(delay, min(refill, RL["tokens_reset"] - now))
    if delay > 0.05:
        if delay > 2:
            print(f"    ... pausing {delay:.0f}s to stay inside the rate limit")
        time.sleep(delay + 0.3)


def compact_history(messages):
    """Shrink old tool output and old manifest bodies; the full text stays in ~/manifest and the cluster."""
    for i in range(1, max(1, len(messages) - COMPACT["keep"])):
        m = messages[i]
        if m["role"] == "tool" and len(m["content"]) > 300:
            m["content"] = m["content"][:300] + " ...[older output trimmed]"
        elif m["role"] == "assistant" and m.get("tool_calls"):
            for tc in m["tool_calls"]:
                if tc["function"]["name"] == "write_manifest":
                    try:
                        a = json.loads(tc["function"]["arguments"])
                        if len(a.get("content", "")) > 80:
                            a["content"] = "[saved to file; use read_manifest to see it]"
                            tc["function"]["arguments"] = json.dumps(a)
                    except Exception:
                        pass


def _create(messages):
    kw = dict(model=MODEL, messages=messages, tools=TOOL_SPECS, tool_choice="auto",
              temperature=0, max_tokens=MAX_OUT)
    if MODEL.startswith("openai/gpt-oss"):
        kw["reasoning_effort"] = "low"                      # fewer hidden reasoning tokens per call
    try:
        raw = llm.chat.completions.with_raw_response.create(**kw)
    except AttributeError:                                  # older SDK without raw-response support
        return llm.chat.completions.create(**kw), {}
    return raw.parse(), raw.headers


def llm_call(messages):
    malformed = 0
    for attempt in range(10):
        compact_history(messages)
        wait_for_budget(messages)
        RL["last_call"] = time.time()
        try:
            resp, headers = _create(messages)
            record_headers(headers)
            used = resp.usage.total_tokens if resp.usage else 0
            STATS["calls"] += 1
            STATS["tokens"] += used
            left = RL["tokens_left"]
            print(f"    [llm] call {STATS['calls']} | {used} tokens"
                  + (f" | {left} left this minute" if left is not None else ""))
            return resp
        except RateLimitError as e:
            text = str(e)
            if "per day" in text.lower() or "(TPD)" in text or "(RPD)" in text:
                raise LimitReached("Groq daily limit reached: " + text[:300])
            wait = float(e.response.headers.get("retry-after") or 20)
            print(f"    ... rate limited, waiting {wait:.0f}s")
            time.sleep(wait + 1)
        except BadRequestError as e:
            if "tool_use_failed" in str(e) or "Failed to call a function" in str(e):
                malformed += 1                               # the model produced a broken tool call
                if malformed > 2:
                    raise
                messages.append({"role": "user", "content":
                                 "Your last tool call was malformed. Call the tool again with valid JSON "
                                 "arguments that match its schema."})
                continue
            raise
        except APIStatusError as e:
            if e.status_code == 413:                         # request bigger than the per-minute budget
                if COMPACT["keep"] > 2:
                    COMPACT["keep"] = 2
                    print("    ... request too large, trimming history harder")
                    continue
                raise LimitReached("A single request exceeds your tokens-per-minute limit. "
                                   "Use a model with a higher limit, or shorten the task.")
            if e.status_code == 404:
                raise LimitReached(f"Model '{MODEL}' is not available to your key. "
                                   "Type /model <name> below (or restart) and then 'continue'.")
            if e.status_code >= 500:
                time.sleep(3 + attempt * 2)
                continue
            raise
        except APIConnectionError:
            time.sleep(3)
    raise RuntimeError("Giving up after repeated Groq errors")


# --------------------------------------------------------------------------
# Agent loop
# --------------------------------------------------------------------------
def run_agent(messages):
    for _ in range(MAX_STEPS):
        try:
            resp = llm_call(messages)
        except LimitReached as e:
            print(f"\nStopped: {e}")
            return
        except Exception as e:
            print(f"\nLLM error: {type(e).__name__}: {str(e)[:400]}")
            return

        msg = resp.choices[0].message
        entry = {"role": "assistant", "content": msg.content or ""}
        if msg.tool_calls:
            entry["tool_calls"] = [{"id": tc.id, "type": "function",
                                    "function": {"name": tc.function.name,
                                                 "arguments": tc.function.arguments or "{}"}}
                                   for tc in msg.tool_calls]
        messages.append(entry)
        if msg.content:
            print(f"\nAgent: {msg.content}")
        if not msg.tool_calls:
            print(f"\n(session total: {STATS['calls']} calls, {STATS['tokens']} tokens)")
            return

        for tc in msg.tool_calls:
            try:
                args = json.loads(tc.function.arguments or "{}")
                args = {k: v for k, v in args.items() if v is not None}
                out = run_tool(tc.function.name, args)
            except json.JSONDecodeError:
                args, out = {}, "Error: arguments were not valid JSON"
            shown = {k: (v if len(str(v)) < 70 else str(v)[:67] + "...") for k, v in args.items()}
            print(f"  [tool] {tc.function.name}({shown}) -> {out[:160].replace(chr(10), ' ')}")
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": out})
    print("Stopped: step limit reached.")


def read_task():
    """Read the task from the terminal. Multi-line paste is fine; finish with /end on its own line."""
    print("\nEnter the task (include the namespace). Type or paste it, then put /end on its own line.")
    lines = []
    while True:
        try:
            line = input("Task> ")
        except EOFError:
            break
        if line.strip() == "/end":
            break
        lines.append(line)
    return "\n".join(lines).strip()


def pick_model():
    """Ask Groq which models this key can use, show them, and propose the best available default."""
    try:
        ids = sorted(m.id for m in llm.models.list().data if getattr(m, "active", True))
    except Exception as e:
        print(f"Could not list models ({type(e).__name__}); you can still type a model name.")
        ids = []
    chat = [i for i in ids if not any(w in i.lower() for w in SKIP_WORDS)]
    default = next((m for m in PREFERRED if m in chat), chat[0] if chat else DEFAULT_MODEL)
    if chat:
        print("Models available to your key: " + ", ".join(chat))
    return input(f"Groq model [Enter = {default}]: ").strip() or default


def main():
    global core, dyn, llm, MODEL

    key = os.environ.get("GROQ_API_KEY") or getpass.getpass("Groq API key: ").strip()
    llm = Groq(api_key=key)
    MODEL = pick_model()

    try:
        contexts, active = config.list_kube_config_contexts()   # reads ~/.kube/config
    except config.ConfigException as e:
        sys.exit(f"Cannot read kubeconfig: {e}")
    names = [c["name"] for c in contexts]
    current = active["name"] if active else names[0]
    print(f"Kube contexts: {', '.join(names)}  (current: {current})")
    chosen = input("Context to use [Enter = current]: ").strip() or current
    if chosen not in names:
        sys.exit(f"Unknown context '{chosen}'")
    config.load_kube_config(context=chosen)
    core = client.CoreV1Api()
    dyn = dynamic.DynamicClient(client.ApiClient())
    print(f"Using model {MODEL} | cluster context {chosen} | manifests dir {MANIFEST_DIR}")

    task = read_task()
    flush_stdin()                                            # discard stray Enter keys left from pasting
    if not task:
        sys.exit("No task entered.")

    messages = [{"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": task}]
    run_agent(messages)

    while True:                                              # follow-up chat, same memory
        follow = input("\nYou (or 'exit'): ").strip()
        if not follow:                                       # a stray Enter must not end the session
            continue
        if follow.lower() in ("exit", "quit"):
            break
        if follow.lower().startswith("/model "):             # switch model without losing the session
            MODEL = follow.split(None, 1)[1].strip()
            print(f"Switched to model {MODEL}. Type 'continue' to resume.")
            continue
        messages.append({"role": "user", "content": follow})
        run_agent(messages)


if __name__ == "__main__":
    main()
