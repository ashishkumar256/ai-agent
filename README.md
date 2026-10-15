# k8s-ai-agent

A small, generic Kubernetes operations agent. You describe a task in plain English; an LLM (served by
[Groq](https://groq.com)) plans the work, writes manifests to disk, applies them to your cluster, waits,
diagnoses failures, fixes them, and validates the result.

- The [Dockerfile](Dockerfile) builds the agent runtime image
- The [Helm chart](charts/k8s-ai-agent/) deploys and configures the Kubernetes resources
- Talks to the cluster through the official `kubernetes` Python client
- Every change needs your approval
- Also in this README: a [free LLM API tiers reference sheet](#free-llm-api-tiers-reference-sheet) (as of 14 October 2026)

## How it works

The agent is a loop around the model:

```
you describe a task
   -> model picks a tool (inspect, write manifest, apply, wait, logs, exec, ...)
   -> your machine runs the tool against the cluster
   -> result goes back to the model
   -> repeat until the model gives a final report (or a step limit is hit)
```

The model decides *what to do next from what it just saw*, for example: pod stuck in `ImagePullBackOff` ->
read events -> fix the manifest -> re-apply. Manifests it writes are saved in `~/manifest`, so you always
end up with the final working YAML on disk.

## Requirements

- Docker, Helm 3, and `kubectl`
- A Groq API key (https://console.groq.com/keys) with access to a tool-calling model
  (for example `openai/gpt-oss-120b`)
- A GitHub token with Contents read/write access if tasks should publish manifests to GitHub
- Cluster permissions for whatever your task needs (RBAC applies as usual)

## Build and deploy with Docker and Helm

The [Dockerfile](Dockerfile) installs the Python dependencies and packages `k8s_agent.py` into the runtime
image. Build and publish the image to a registry your cluster can pull from:

```bash
docker build -t ghcr.io/YOUR_ORG/k8s-ai-agent:latest .
docker push ghcr.io/YOUR_ORG/k8s-ai-agent:latest
```

The chart creates a credentials Secret when either credential is supplied. The Groq key is required by the
agent; the GitHub token is optional and is only needed to publish manifests. Enter the keys as plain text,
without base64 encoding, and without echoing them. The chart applies Helm's `b64enc` to populate Kubernetes
Secret `data`; then install the chart, which creates the namespace and chart resources:

```bash
read -r -s -p "Groq API key: " GROQ_API_KEY
echo
read -r -s -p "GitHub token (optional; press Enter to skip): " GITHUB_TOKEN
echo
helm upgrade --install k8s-ai-agent ./charts/k8s-ai-agent \
  --namespace k8s-ai-agent \
  --create-namespace \
  --set image.repository=ghcr.io/YOUR_ORG/k8s-ai-agent \
  --set image.tag=latest \
  --set-string credentials.groqApiKey="$GROQ_API_KEY" \
  --set-string credentials.githubToken="$GITHUB_TOKEN"
unset GROQ_API_KEY GITHUB_TOKEN

kubectl -n k8s-ai-agent attach -it $(kubectl -n k8s-ai-agent get pod -l app.kubernetes.io/instance=k8s-ai-agent -o name | head -n 1)
```

The chart creates the Namespace (through `--create-namespace`), Deployment, ServiceAccount, ClusterRole,
and ClusterRoleBinding. When either credential is non-empty, it creates a Secret with only the supplied
key or keys and references it from the Deployment. If both are empty, the Deployment references the
existing Secret named by `existingCredentialsSecret` (default `agent-credentials`). Helm stores supplied
credential values in release metadata, so restrict access to the Helm release and use a cluster secret
manager for production.

Common chart settings can be overridden with `--set` or a values file:

| Setting | Default | Purpose |
|---|---|---|
| `image.repository` | `k8s-ai-agent` | Container image repository |
| `image.tag` | `latest` | Container image tag |
| `groq.model` | `openai/gpt-oss-120b` | Groq chat model |
| `replicaCount` | `1` | Number of agent pods |
| `credentials.groqApiKey` | `""` | Groq key; a non-empty value causes chart-managed Secret creation |
| `credentials.githubToken` | `""` | Optional GitHub token; a non-empty value causes chart-managed Secret creation |
| `existingCredentialsSecret` | `agent-credentials` | Secret referenced only when both credential values are empty |
| `rbac.create` | `true` | Create the chart's ClusterRole and ClusterRoleBinding |
| `serviceAccount.create` | `true` | Create the ServiceAccount used by the agent |

Enter the task at the prompt. The pod uses its ServiceAccount token through the Kubernetes in-cluster
configuration; it does not need a mounted kubeconfig. It remains interactive because approvals and the
task are entered in the terminal. Detaching does not stop the process; reconnect with the attach command.
The generated manifests live in the pod's temporary `emptyDir` and are lost if the pod is replaced.
After a task reports `TASK STATUS: PASS`, choose whether to keep its generated manifests in the pod,
discard them, or push them to GitHub. Include `repository: owner/name` and `branch: branch-name` in the task
when requesting a push. Publishing creates one commit containing only manifests written during that task,
under `manifests/` in the target repository; use `.` at the publish prompt to put them at the repository
root. If the requested branch does not exist, the agent creates it from the repository's default branch.
Files containing Kubernetes Secret resources are published as redacted learning examples named
`*.yaml.example`: secret values become `REPLACE_BEFORE_USE`, the resource name gets an `-example` suffix,
and a redaction annotation is added. The real password in the live Secret manifest is never committed.
The GitHub token is used only for the publish request and is never included in the commit.

The chart's custom ClusterRole grants an explicit allowlist of workload permissions across namespaces,
plus read-only discovery for namespaces and StorageClasses. It allows namespace create/patch, common
workload CRUD, pod logs and exec, and Secret creation. The agent still asks before using each namespace.
It cannot manage arbitrary resource kinds or CRDs unless their permissions are added to the chart's RBAC
template. The agent can execute commands in pods and create Secrets, so treat its credentials and workload
like privileged automation. Kubernetes RBAC cannot enforce the Python approval prompts. Installing the
chart requires permission to create ClusterRoles and ClusterRoleBindings.

## Example task: highly available PostgreSQL 16

Paste this at the `Task>` prompt, then `/end`:

```
Provision and validate a highly available 3-replica PostgreSQL 16 StatefulSet in namespace `middleware`
with 20Gi storage per replica, using the default StorageClass (do not set storageClassName).
Use the official postgres:16 image. HA means real streaming replication: postgres-0 is the primary,
postgres-1 and postgres-2 are standbys built with pg_basebackup. Include a Secret manifest with the
password in stringData and reference it with secretKeyRef. Add a headless Service and a postgres-primary Service.
Validate: 3/3 pods Ready, PVCs Bound at 20Gi on the default class, server version 16,
postgres-0 not in recovery with 2 streaming replicas, standbys in recovery, replication lag near zero.
```

Typical flow: check namespace and default StorageClass -> write `00-namespace.yaml` -> apply (you approve) ->
create the secret (you approve) -> write and apply services and StatefulSet -> wait for readiness ->
run read-only SQL checks in each pod -> final PASS/FAIL report.

Be specific in your task. A one-line task lets the model choose its own design, which may not be what you
want (for example, three independent databases instead of real replication).

## Tools the model can call

| Tool | What it does | Needs approval |
|---|---|---|
| `namespace_overview` | One-call summary of StatefulSets, Deployments, Pods, PVCs, Services | no |
| `k8s_get` | Get or list any kind (including CRDs); `path` returns only part of an object | no |
| `k8s_events` | Recent events for a namespace or object | no |
| `pod_logs` | Container logs, including previous crashed container | no |
| `write_manifest` | Save YAML to `~/manifest/<file>`; Secret values are stored locally and redacted on GitHub publish | no |
| `read_manifest` | Read a saved manifest | no |
| `apply_manifest` | Server-side apply of a saved manifest file | **yes** (`a` = always this run) |
| `wait_until_ready` | Wait for StatefulSet/Deployment/Pod/Job; returns early with evidence if pods are stuck | no |
| `pod_exec` | Run a command (argv list, no shell) in one or several pods | **yes** (`a` = always this run) |
| `k8s_delete` | Delete one namespaced object | **yes, every time** |

`wait_until_ready` polls inside Python (no LLM calls) and recognizes `ImagePullBackOff`, `ErrImagePull`,
`CrashLoopBackOff`, `CreateContainerConfigError`, `InvalidImageName`, and `Unschedulable` pods.

## Safety model

- **Namespaces are approved on first use.** The first time the agent touches a namespace you get
  `Allow the agent to use namespace 'x'? [y/N]`. Applying a `Namespace` manifest that you approved counts too.
- **Every change asks first**: apply, exec, delete. Secret creation uses the normal `apply_manifest` approval.
- **Deletes always ask**, even if you answered `a` for other tools.
- **Cluster-scoped objects are blocked**, except `Namespace`. Deleting cluster-scoped objects is not possible.
- **The running agent has no host shell or kubectl.** The operator uses `kubectl` to install the Helm
  chart, create the credentials Secret, and attach to the pod; the agent itself talks to Kubernetes via
  the Python client and only writes task manifests into its `~/manifest` directory.
- **Secret values are exposed in this mode.** Secret YAML is saved locally and its values are included in model
  tool history. GitHub publishing replaces values with `REPLACE_BEFORE_USE` in an `*.yaml.example` file, but
  protect local manifests and avoid pushing raw Secret YAML outside the agent's publish workflow.
- **Step limit** (`MAX_STEPS`, default 60) and a retry limit on failures.
- Stray keystrokes (for example from pasting) are discarded before each approval prompt.

Still: you are giving an LLM a path to your cluster. Read each approval prompt, and try new tasks on a test
cluster (kind, minikube, a lab) before anything important.

## Rate limits (Groq)

The agent is built for per-minute token and request limits:

- Reads Groq's `x-ratelimit-remaining-tokens` header after each call and pauses just long enough when the next
  request would not fit (`pausing Ns to stay inside the rate limit`)
- Spaces requests to stay under `MAX_RPM`
- On HTTP 429 waits for `retry-after` and retries; on a daily limit it stops with a clear message
- On HTTP 413 (request too large) trims history harder and retries once
- Retries when a model produces a malformed tool call
- Keeps token use low: short prompts and tool descriptions, tool output capped, older tool output and old
  manifest bodies trimmed from history (full text stays in `~/manifest` and the cluster), `reasoning_effort=low`
  for `openai/gpt-oss-*` models
- Prints tokens used and tokens left this minute after every call, and session totals at the end

Check your real limits at https://console.groq.com/settings/limits. Free-tier limits are small, so expect
pauses; a paid tier removes most of the waiting.

## Configuration

Constants at the top of `k8s_agent.py`:

| Constant | Default | Meaning |
|---|---|---|
| `DEFAULT_MODEL` | `openai/gpt-oss-120b` | Fallback if the model list cannot be fetched |
| `PREFERRED` | list | Order used to propose a default from the models your key has |
| `MAX_OUT` | 1500 | Max completion tokens per call |
| `MAX_RPM` | 25 | Requests per minute the agent allows itself |
| `KEEP_FULL` | 8 | Newest messages kept in full; older tool output is trimmed |
| `MAX_TOOL_OUTPUT` | 2500 | Max characters of any tool result sent to the model |
| `MAX_STEPS` | 60 | Max model calls per task or follow-up |

## Files and folders

| Path | Purpose |
|---|---|
| `k8s_agent.py` | Agent application source |
| `charts/k8s-ai-agent/Chart.yaml` | Helm chart metadata |
| `charts/k8s-ai-agent/values.yaml` | Configurable image, credentials, RBAC, and pod defaults |
| `charts/k8s-ai-agent/templates/secrets.yaml` | Optional chart-managed credentials Secret |
| `charts/k8s-ai-agent/templates/rbac.yaml` | ServiceAccount, ClusterRole, and ClusterRoleBinding |
| `charts/k8s-ai-agent/templates/deployment.yaml` | Agent Deployment, security context, resources, and writable volumes |
| `~/manifest/` | Task manifests written by the running agent; pod-local in Kubernetes deployments |

## Troubleshooting

| Symptom | Fix |
|---|---|
| `model_not_found` / 404 | Pick a model from the printed list, or type `/model <name>` then `continue` |
| `pausing Ns to stay inside the rate limit` | Normal; the agent is waiting for your per-minute token budget |
| `Groq daily limit reached` | Wait for the reset, use another key, or move to a paid tier |
| `A single request exceeds your tokens-per-minute limit` | Use a model with a higher limit or shorten the task |
| Repeated malformed tool calls | Switch model with `/model` |
| `403 Forbidden` | Your kube user lacks RBAC for that action |
| `server_side_apply` attribute error | Rebuild and push the image, then run `helm upgrade --install` with the same chart settings |
| PVC stays `Pending` | No default StorageClass or its provisioner is not working; ask the agent to check |
| `ImagePullBackOff` | The agent reports it early with the event message. Wrong tag: it fixes it. Nodes cannot reach the registry: fix networking, no manifest change helps |
| Pod still broken after the agent fixed a StatefulSet | A StatefulSet does not replace a not-ready pod after a spec change; the agent deletes the stuck pod (you approve) so it is recreated |
| Agent asks which namespace | Put the namespace in the task |
| Starting over | `kubectl delete namespace <ns>` (removes its PVCs), then run again |

## Limitations

- **The PostgreSQL example gives streaming replication, not automatic failover.** If the primary dies,
  nothing promotes a standby. For production use Patroni or an operator such as CloudNativePG.
- Manifests are written by the model. First attempts can contain mistakes; the diagnose-fix-reapply loop
  exists for that, and a stronger model gives better first attempts.
- Validation is only as good as the checks in your task. State what "validated" means.
- Not a replacement for GitOps or review. Treat `~/manifest` as a draft to review before reuse.

## Extending

Add a capability in three steps:

1. Write a Python function that returns a short string (errors are returned to the model as text).
2. Add it to the `TOOLS` dict.
3. Add a `tool(...)` entry to `TOOL_SPECS` with a clear, short description. Models choose tools from these
   descriptions, and they are resent on every call, so keep them tight.

If the tool changes state, add its name to `MUTATING` so it asks for approval (and to `ALWAYS_ASK` to
disable the "always" shortcut). Because the tools are generic Kubernetes operations, the same agent handles
other tasks: deploy an app, add an Ingress, debug a CrashLoopBackOff, set up a CronJob, and so on.

---

## Free LLM API tiers (reference sheet)

[![Free LLM APIs](https://img.shields.io/badge/Free%20LLM%20APIs-1F3864?style=for-the-badge)](#free-llm-apis) [![Budget Calculator](https://img.shields.io/badge/Budget%20Calculator-1F3864?style=for-the-badge)](#budget-calculator) [![About](https://img.shields.io/badge/About-1F3864?style=for-the-badge)](#about)

> **Date notice:** treat this sheet as true as of **14 October 2026**, the date its content was created. Free-tier limits change
> often and trackers sometimes disagree, so confirm every number in the provider's own dashboard before relying on it.

### Free LLM APIs

*Columns: RPM = requests per minute, RPD = requests per day, TPM = tokens per minute, TPD = tokens per day.
A dash means the provider publishes no single comparable figure. Data as of 14 October 2026.*

| Provider | What's free | RPM | RPD | TPM | TPD | Card needed? | Best for | Source |
|---|---|--:|--:|--:|--:|---|---|---|
| **Groq** | Free plan on open models (e.g. gpt-oss-120b, Qwen3.8 27B) | 30 | 1,000 | 8,000 | 200,000 | No | Tool-calling agents, fast experiments | [link](https://console.groq.com/docs/rate-limits) |
| **Google Gemini (AI Studio)** | Flash and Flash-Lite models (Pro models paid since Apr 2026) | 5 | 100 | 250,000 | — | No billing needed to start | Most requests per day with Flash-Lite | [link](https://ai.google.dev/gemini-api/docs/rate-limits) |
| **OpenRouter** | Models whose ID ends in :free (about 25 as of Jul 2026) | 20 | 50 | — | — | No | Trying many models with one key | [link](https://openrouter.ai/docs/limits) |
| **Mistral (Experiment plan)** | Mistral open and proprietary models | 60 | — | 500,000 | — | No card; phone verification required | Large monthly token budget | [link](https://help.mistral.ai/en/articles/450104-how-can-i-try-the-api-for-free-with-the-experiment-plan) |
| **GitHub Models** | Any GitHub account with a personal access token | 15 | 150 | — | — | No | Quick start if you already use GitHub | [link](https://docs.github.com/en/billing/concepts/product-billing/github-models) |
| **Cloudflare Workers AI** | Daily compute allowance on open models | — | — | — | — | No (card only to exceed the free amount) | Small edge/web projects | [link](https://developers.cloudflare.com/workers-ai/platform/pricing/) |
| **NVIDIA (build.nvidia.com)** | Hosted open models via the NVIDIA developer program | 40 | — | — | — | No | Trying large open models | [link](https://forums.developer.nvidia.com/t/is-nim-free/369368) |
| **SambaNova** | Free tier on open models (DeepSeek, Llama, gpt-oss, others) | 20 | 20 | — | 200,000 | No | Quick tests of large open models | [link](https://docs.sambanova.ai/docs/en/models/rate-limits) |
| **Cerebras** | Free trial tier on open models | 5 | — | 90,000 | 1,000,000 | Verify: one tracker says a payment method is needed to activate | Speed experiments | [link](https://inference-docs.cerebras.ai/support/rate-limits) |
| **Cohere** | Trial key (all models, non-production) | 20 | — | — | — | No | Embedding and rerank experiments | [link](https://docs.cohere.com/docs/going-live) |
| **Hugging Face Inference Providers** | Small monthly credit | — | — | — | — | No | A few test calls across providers | [link](https://huggingface.co/docs/inference-providers/pricing) |
| **Ollama (local)** | Open models on your own machine | — | — | — | — | No | Unlimited tinkering, private data | [link](https://ollama.com) |
| **Anthropic API** | No lasting free tier | — | — | — | — | Phone verification | n/a | [link](https://console.anthropic.com) |
| **OpenAI API** | Sources conflict | — | — | — | — | Check your account | n/a | [link](https://developers.openai.com/api/docs/guides/rate-limits) |

**Notes and catches** (as of 14 October 2026)

| Provider | Other limits and notes | Catch |
|---|---|---|
| **Groq** | Sample limits for gpt-oss-120b and Qwen. Compound models: 250 requests/day, 70,000 tokens/min. Your exact limits: console.groq.com/settings/limits | Tokens per minute is the tight limit. Rate-limit headers come back on every response. |
| **Google Gemini (AI Studio)** | Numbers here are the conservative LOW end of third-party reports (5-15 RPM, 100-1,000 RPD); limits vary by model and project. Check AI Studio for yours. | Free-tier inputs may be used to improve Google products. Free tier only in eligible countries. Limits were cut in late 2025. |
| **OpenRouter** | Daily limit rises to 1,000 once you have bought $10+ of credits (one-time threshold). | Failed requests can count toward the daily limit. Free model list rotates. A negative balance returns 402 even on free models. |
| **Mistral (Experiment plan)** | About 1 request/second per model (shown as 60/min); about 1B tokens/month per model (third-party figure; the Mistral console is the source of truth). | Requests on the Experiment plan may be used to train Mistral models. |
| **GitHub Models** | Low-tier models: 15 RPM / 150 RPD. High-tier models: 10 RPM / 50 RPD. 8k input + 4k output tokens per request. 5 concurrent requests (2 for high-tier). | Small per-request size; limits subject to change; meant for prototyping. |
| **Cloudflare Workers AI** | 10,000 neurons/day, resets 00:00 UTC. Neurons depend on the model and size, so tokens per day vary a lot. | Neurons are not tokens, so capacity is hard to predict. |
| **NVIDIA (build.nvidia.com)** | Your limit is shown in the dashboard and varies by model and load. Early signup credits (1,000) have been replaced by rate limits. | Intended for prototyping and development, not production. No official way to raise free-tier limits. |
| **SambaNova** | Free tier applies when no payment method is linked to the account. | Only about 20 requests/day, which is too few for agent loops. Model list changes. |
| **Cerebras** | 30,000 uncached input tokens/min; 1M tokens/hour; $5 signup credit that expires after 30 days (per one tracker). | Time-limited trial rather than a permanent free tier. Very fast inference. |
| **Cohere** | Rerank: 10 requests/min. 1,000 API calls per month in total across endpoints. | Not allowed for production or commercial use. |
| **Hugging Face Inference Providers** | Free users: under $0.10/month. PRO: $2/month. | Credits run out almost immediately; no pay-as-you-go on the free plan. |
| **Ollama (local)** | No token quota; limited only by your hardware. | Speed and model size depend on your CPU/GPU/RAM. |
| **Anthropic API** | New API accounts may get a small one-time starter credit (reported about $5). | Starter credit is not a public per-key free tier. The claude.ai chat free tier is separate. |
| **OpenAI API** | One tracker lists a free usage tier for unpaid accounts in eligible countries; others say free trial credits were discontinued in mid-2025. | Treat as paid unless your dashboard shows credit. |

[Back to buttons](#free-llm-api-tiers-reference-sheet)

### Budget Calculator

How many agent tasks fit in each free tier? Inputs used for the table below (edit them in the Excel workbook to
recalculate):

- **Tokens per LLM call (input + output):** 1,500. Example value; a k8s agent run on Groq used about 1,100 tokens per call.
- **LLM calls per task:** 15. Assumption: about 15 model calls to provision and validate one workload.

How to read it: *Tasks/day (request limit)* = requests per day ÷ calls per task. *Tasks/day (token limit)* = tokens per
day ÷ (tokens per call × calls per task). *Max tasks/day* is whichever limit runs out first. *Calls/min sustainable* is
the lower of RPM and TPM ÷ tokens per call. *Minutes per task* = calls per task ÷ that pace. Gemini uses the
conservative low end of third-party reports. Data as of 14 October 2026.

| Provider | RPM | RPD | TPM | TPD | Tasks/day (request limit) | Tasks/day (token limit) | Max tasks/day | Calls/min sustainable | Minutes per task |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| **Groq** | 30 | 1,000 | 8,000 | 200,000 | 66.7 | 8.9 | 8.9 | 5.3 | 2.8 |
| **Google Gemini (AI Studio)** | 5 | 100 | 250,000 | — | 6.7 | — | 6.7 | 5 | 3 |
| **OpenRouter** | 20 | 50 | — | — | 3.3 | — | 3.3 | 20 | 0.8 |
| **Mistral (Experiment plan)** | 60 | — | 500,000 | — | — | — | — | 60 | 0.2 |
| **GitHub Models** | 15 | 150 | — | — | 10 | — | 10 | 15 | 1 |
| **Cloudflare Workers AI** | — | — | — | — | — | — | — | — | — |
| **NVIDIA (build.nvidia.com)** | 40 | — | — | — | — | — | — | 40 | 0.4 |
| **SambaNova** | 20 | 20 | — | 200,000 | 1.3 | 8.9 | 1.3 | 20 | 0.8 |
| **Cerebras** | 5 | — | 90,000 | 1,000,000 | — | 44.4 | 44.4 | 5 | 3 |
| **Cohere** | 20 | — | — | — | — | — | — | 20 | 0.8 |
| **Hugging Face Inference Providers** | — | — | — | — | — | — | — | — | — |
| **Ollama (local)** | — | — | — | — | — | — | — | — | — |
| **Anthropic API** | — | — | — | — | — | — | — | — | — |
| **OpenAI API** | — | — | — | — | — | — | — | — | — |

[Back to buttons](#free-llm-api-tiers-reference-sheet)

### About

Compiled 14 Oct 2026 from provider documentation and third-party trackers (BenchLM and similar). Free-tier limits change often and sources sometimes disagree, so treat every number as approximate and confirm it in the provider's own dashboard.

**Privacy note**

Free tiers from Google (Gemini) and Mistral may use your prompts to improve or train their models. Do not send real secrets, customer data or cluster credentials through them.

**Assumptions and caveats**

- Gemini limits: low end of reported ranges (5 RPM, 100 RPD, 250K TPM for Flash); Google's own page lists limits per model.
- Mistral: 1 request/second is shown as 60 requests/min. Mistral's console is the source of truth for exact limits.
- GitHub Models: the numeric columns show low-tier models; high-tier models are lower (see Other limits).
- Cerebras: one tracker reports a payment method is needed to activate the free credit; verify before signing up.
- OpenAI: sources conflict on whether any free usage exists; check your own account.
- Blank numeric cells mean no single published figure applies (for example Cloudflare neurons or Hugging Face credits).

**Main sources**

- [Groq rate limits](https://console.groq.com/docs/rate-limits)
- [Gemini API rate limits](https://ai.google.dev/gemini-api/docs/rate-limits)
- [OpenRouter limits](https://openrouter.ai/docs/limits)
- [Mistral Experiment plan](https://help.mistral.ai/en/articles/450104-how-can-i-try-the-api-for-free-with-the-experiment-plan)
- [GitHub Models billing and free use](https://docs.github.com/en/billing/concepts/product-billing/github-models)
- [Cloudflare Workers AI pricing](https://developers.cloudflare.com/workers-ai/platform/pricing/)
- [Hugging Face Inference Providers pricing](https://huggingface.co/docs/inference-providers/pricing)
- [SambaNova rate limits](https://docs.sambanova.ai/docs/en/models/rate-limits)
- [Cohere going live and trial keys](https://docs.cohere.com/docs/going-live)
- [BenchLM free-tier tracker](https://benchlm.ai/free-tier)

[Back to buttons](#free-llm-api-tiers-reference-sheet)