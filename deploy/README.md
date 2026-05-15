# VirtValidate — Deployment Guide

VirtValidate ships in three flavors. Pick the one that matches your
infrastructure; the application code is identical across all of them.

| Option | Best for | Path |
|--------|----------|------|
| **Standalone (podman-compose)** | Single-host, dev/test, demos, air-gapped lab | repo root |
| **OpenShift via Helm** | Enterprise OCP, GitOps via release tags, value-driven config | `deploy/helm/virtvalidate/` |
| **OpenShift via Kustomize** | ArgoCD-managed deployments, audit-by-diff workflows | `deploy/kustomize/` |

For RHOAI / KServe deployments (federal, multi-tenant inference), use
the Helm `--set llm.backend=kserve …` invocation or the Kustomize
`overlays/rhoai-kserve` overlay — both produce the same set of
resources.

---

## Option 1: Standalone Appliance (podman-compose)

**Best for:** single-host, dev/test, customer demos, air-gapped lab
deployments where one machine runs the whole stack.

```bash
# From the repo root
podman-compose up -d
```

That brings up four containers (frontend, backend, postgres, ollama),
binds port 3000 (UI) + 8000 (API) on the host, and persists data on
named volumes.

### Prerequisites
- Podman 4.0+ (rootless preferred) or Docker
- 8 GiB RAM (Ollama with `llama3:8b` is the floor — bigger models need more)
- 30 GiB free disk (model weights + Postgres data + container layers)
- Linux host with `:Z` SELinux relabel support (RHEL/Fedora) or `:z` for Docker on macOS

### What you get
- Dashboard on `http://localhost:3000`
- API on `http://localhost:8000`
- Postgres exposed on `localhost:5432` for local introspection
- All container logs via `podman-compose logs -f`

### Configuration
Override defaults via `.env` at the repo root. Every variable from
`docs/CONFIGURATION.md` works. For local dev, the existing
`podman-compose.yml` bind-mounts `backend/app` and `backend/migrations`
into the running container so uvicorn's `--reload` flag picks up
edits without a rebuild.

### Upgrade
```bash
git pull
podman-compose pull   # if you want fresh images
podman-compose up -d  # recreates containers with new manifests
```

### Backup
- **Postgres:** `podman exec virtvalidation_postgres_1 pg_dump -U virtvalidate virtvalidate > backup.sql`
- **SSH keys:** `podman exec virtvalidation_backend_1 tar -czf - /app/keys > keys-backup.tar.gz`

### Limitations
- Single-host — no high availability. The pod that crashes takes the appliance down with it.
- No FIPS compliance — the container's OpenSSL is not validated when running on a non-FIPS host. See `docs/FIPS_DEPLOYMENT.md` for the federal-deployment path.

---

## Option 2: OpenShift via Helm

**Best for:** enterprise OCP deployments, multi-environment value
overrides (dev/staging/prod values files), customers already on
release-please / Helm-OCI workflows.

### Quick start (Ollama backend, in-cluster)

```bash
# Default — backend talks to an Ollama pod the chart deploys.
helm install virtvalidate deploy/helm/virtvalidate/ \
  --namespace virtvalidate \
  --create-namespace
```

### Quick start (KServe backend, RHOAI)

```bash
helm install virtvalidate deploy/helm/virtvalidate/ \
  --namespace virtvalidate \
  --create-namespace \
  --set llm.backend=kserve \
  --set llm.kserve.endpoint=https://granite.rhoai.svc.cluster.local \
  --set llm.kserve.modelName=granite-3-8b-instruct
```

### Quick start (MaaS backend — authenticated OpenAI-compatible)

For deployments that consume an external Model-as-a-Service endpoint
(LiteLLM proxy, OpenRouter, hosted vLLM behind a reverse-proxy, etc.):

```bash
# 1. Operator pre-creates the Secret holding the API key.
oc create secret generic virtvalidate-maas \
  --from-literal=api-key='<YOUR_KEY>' \
  -n virtvalidate

# 2. Install with MaaS connection config wired up.
helm install virtvalidate deploy/helm/virtvalidate/ \
  --namespace virtvalidate \
  --create-namespace \
  --set llm.maas.enabled=true \
  --set llm.maas.baseUrl=https://litellm-prod.apps.maas.redhatworkshops.io/v1 \
  --set llm.maas.model=granite-32-8b-instruct \
  --set llm.maas.existingSecret=virtvalidate-maas
```

The base URL follows OpenAI client convention and INCLUDES the `/v1`
prefix.

After install, switch the active backend in the VirtValidate Settings
UI: **Settings → LLM Backend → MaaS → Make active**. Use **Test
connection** first to verify reachability + auth + that the configured
model is in the endpoint's `/models` list.

The active backend lives in `app_settings.active_llm_backend` and
flips at runtime — no redeploy needed. CONNECTION CONFIG (URLs,
Secret name) stays in Helm values; the Settings UI only controls
which configured backend is currently active.

> **Never set `llm.maas.apiKey` in a committed values.yaml.** The
> `--set llm.maas.apiKey=<KEY>` path templates a chart-managed Secret
> for one-shot installs but writing the key into a values file leaks
> it to git history and CI logs. Production: always use
> `existingSecret` with a Vault / ESO / sealed-secrets-managed Secret.

### Quick start (production, FIPS, NetworkPolicies)

```bash
helm install virtvalidate deploy/helm/virtvalidate/ \
  --namespace virtvalidate-prod \
  --create-namespace \
  --values values-prod.yaml \
  --set route.enabled=true \
  --set route.host=virtvalidate.apps.your-cluster.com \
  --set networkPolicies.enabled=true \
  --set podDisruptionBudget.enabled=true \
  --set security.fipsMode=true \
  --set security.sshKeyAlgorithm=rsa-3072
```

### Prerequisites
- OpenShift 4.12+ (vanilla Kubernetes 1.25+ also works; use `--set ingress.enabled=true` instead of `route.enabled`)
- Helm 3.10+
- A StorageClass for the SSH keys PVC and Postgres PVC (defaults to the cluster's default class)
- For KServe: an InferenceService exposing the OpenAI-compatible `/v1/chat/completions` API
- For FIPS: see [`../docs/FIPS_DEPLOYMENT.md`](../docs/FIPS_DEPLOYMENT.md)

### Resource requirements

| Component | Requests (default) | Limits (default) |
|-----------|--------------------|------------------|
| Frontend  | 50m CPU / 64 MiB RAM   | 200m CPU / 256 MiB RAM |
| Backend   | 100m CPU / 256 MiB RAM | 1 CPU / 1 GiB RAM      |
| Postgres  | 100m CPU / 256 MiB RAM | 1 CPU / 1 GiB RAM      |
| Ollama    | 500m CPU / 4 GiB RAM   | 4 CPU / 16 GiB RAM     |

For KServe deployments, exclude Ollama — the InferenceService runs separately.
Production overlays bump backend to 500m / 1 GiB requests.

### Network requirements
- Backend pod → Postgres pod (in-namespace): port 5432
- Backend pod → Ollama pod (in-namespace) or KServe endpoint (cluster or external): port 11434 / 443
- Backend pod → managed VMs (out-of-cluster): port 22 (SSH)
- Frontend pod → Backend pod (in-namespace): port 8000
- Ingress / Route → Frontend pod: port 3000

When `networkPolicies.enabled=true`, the chart adds policies that lock
down ingress to the above paths. SSH egress to managed VMs is left
open by default; tighten by setting `networkPolicies.defaultDenyEgress=true`
and adding an allowlist in your overlay's NetworkPolicy.

### Storage requirements
- SSH keys PVC: 64 MiB (just the key + persistent known_hosts)
- Postgres PVC: 20 GiB default (sized for ~10k VMs and a year of validation history)
- Ollama models PVC: 20 GiB default (`llama3:8b` is ~5 GiB; larger models need more)

For FIPS deployments, the StorageClass MUST support encryption at rest.

### Security considerations
- **Secrets:** the chart generates a Postgres password by default. Production must set `postgres.auth.existingSecret` to a Secret managed by Vault, External Secrets Operator, or sealed-secrets.
- **TLS:** Routes terminate at the OCP edge. The application doesn't terminate TLS itself. Internal traffic is plaintext (Postgres, KServe).
- **SSH keys:** The first launch generates an Ed25519 keypair. For FIPS deployments, replace it with RSA-3072 or ECDSA P-384 before triggering the first capture (see `docs/SSH_SETUP.md` and `docs/FIPS_DEPLOYMENT.md`).
- **PodSecurityContext:** Defaults match OpenShift's `restricted-v2` SCC — runs as a non-root arbitrary high UID, no capabilities, seccompProfile=RuntimeDefault.

### Upgrade
```bash
helm upgrade virtvalidate deploy/helm/virtvalidate/ \
  --namespace virtvalidate \
  --reuse-values \
  --set image.tag=0.2.0
```

The chart's `Recreate` strategy on the backend means a brief outage
during upgrade — the SSH keys PVC is `ReadWriteOnce` so two pods can't
hold it. Operators on `RWX` storage patch the strategy to `RollingUpdate`.

### Backup
- Postgres: `oc exec -n <ns> -c postgres virtvalidate-postgres-0 -- pg_dump …`
- PVCs: use Velero or your storage class's native snapshot facility
- The chart-managed Postgres Secret should NOT be backed up — re-derive on restore.

### Troubleshooting

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| `helm install` errors with `llm.kserve.endpoint is required` | Selected `llm.backend=kserve` without setting the endpoint | Add `--set llm.kserve.endpoint=…` |
| Backend pod CrashLoopBackOff with `connection refused` to Postgres | Postgres pod still initializing | Wait ~30s for the StatefulSet to come up; check `oc logs sts/virtvalidate-postgres` |
| Ollama pod stuck pulling image / OOMKilled | Model larger than memory limit | Bump `llm.ollama.resources.limits.memory` |
| Settings page shows "FIPS mismatch" warning | Application configured for FIPS but host OS isn't | Run `fips-mode-setup --enable` on the cluster nodes (RHEL) or redeploy with `fips: true` (RHCOS) |

---

## Option 3: OpenShift via Kustomize

**Best for:** ArgoCD GitOps workflows, audit-by-diff (every change is a
real YAML diff in version control), multi-cluster fleets where a single
overlay tree feeds N targets.

### Quick start

```bash
# Standalone Ollama, namespace virtvalidate
kubectl apply -k deploy/kustomize/overlays/standalone-ollama

# RHOAI / KServe — edit the overlay's deployment-backend-kserve.yaml
# to set the actual endpoint, then:
kubectl apply -k deploy/kustomize/overlays/rhoai-kserve

# Production hardening overlay
kubectl apply -k deploy/kustomize/overlays/production

# Local dev (k3d / kind / OCP-CRC)
kubectl apply -k deploy/kustomize/overlays/dev
```

### Layout

```
deploy/kustomize/
├── base/                          # shared manifests, no env-specific config
│   ├── kustomization.yaml
│   ├── configmap.yaml
│   ├── deployment-backend.yaml
│   ├── deployment-frontend.yaml
│   ├── pvc-ssh.yaml
│   ├── secret-postgres.yaml
│   ├── service-backend.yaml
│   ├── service-frontend.yaml
│   ├── service-postgres.yaml
│   ├── serviceaccount.yaml
│   └── statefulset-postgres.yaml
└── overlays/
    ├── standalone-ollama/         # base + Ollama Deployment + Service + PVC
    ├── rhoai-kserve/              # base + KServe RBAC + ConfigMap/env patches
    ├── production/                # standalone-ollama + NetworkPolicies + PDB + tighter resources
    └── dev/                       # standalone-ollama + Always pull + minimal resources
```

### Customizing for your environment

1. **Postgres credentials** — `base/secret-postgres.yaml` has `changeme`. Replace before applying, or override with a SecretGenerator in your overlay sourced from a sealed-secret / ESO.
2. **Image tags** — `base/kustomization.yaml` pins to `0.1.0-alpha`. Production overlays pin per-release; dev tracks `latest`.
3. **Namespace** — each overlay sets its own (`virtvalidate`, `virtvalidate-prod`, `virtvalidate-dev`). Edit if conflicting with an existing namespace.
4. **KServe overlay** — `overlays/rhoai-kserve/deployment-backend-kserve.yaml` has placeholder values. Set the actual `KSERVE_ENDPOINT` and `KSERVE_MODEL_NAME` before applying.

### ArgoCD usage

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: virtvalidate
spec:
  source:
    repoURL: https://github.com/virtvalidate/virtvalidate
    path: deploy/kustomize/overlays/production
    targetRevision: v0.1.0
  destination:
    server: https://kubernetes.default.svc
    namespace: virtvalidate-prod
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
```

### Validation
```bash
# Render the full manifest set without applying — useful for diffing
# before sync or in PR review:
kubectl kustomize deploy/kustomize/overlays/production
```

---

## Option 4: OpenShift + RHOAI (KServe inference)

**Best for:** federal customers, organizations with existing RHOAI
investments, multi-tenant inference where the LLM serves multiple apps.

### Helm path

```bash
helm install virtvalidate deploy/helm/virtvalidate/ \
  --namespace virtvalidate \
  --create-namespace \
  --set llm.backend=kserve \
  --set llm.kserve.endpoint=https://granite.rhoai.svc.cluster.local \
  --set llm.kserve.modelName=granite-3-8b-instruct \
  --set route.enabled=true \
  --set route.host=virtvalidate.apps.your-cluster.com
```

When the application pod runs in OpenShift, the SA token at
`/var/run/secrets/kubernetes.io/serviceaccount/token` is read on every
KServe request — no token configuration needed. For external KServe
endpoints, set `llm.kserve.tokenSecret` to a Secret containing the bearer
token.

### Kustomize path

```bash
# Edit the placeholder URL first:
$EDITOR deploy/kustomize/overlays/rhoai-kserve/deployment-backend-kserve.yaml

kubectl apply -k deploy/kustomize/overlays/rhoai-kserve
```

### Prerequisites (RHOAI)
- OpenShift 4.14+
- Red Hat OpenShift AI (RHOAI) 2.10+
- An InferenceService deployed and ready with a model that supports OpenAI-compatible chat completions (vLLM, TGIS, or similar predictor runtime)
- ServiceAccount in the VirtValidate namespace with read access to the InferenceService (the Kustomize overlay creates this; the Helm chart does it conditionally)

### What the InferenceService must expose
- HTTPS on the predictor route
- OpenAI-compatible `/v1/chat/completions` endpoint
- `/v1/models` endpoint listing the served model id (used by VirtValidate's `list_models` and Settings UI)

For models that only expose KServe's native v1 inference protocol (raw
predict/explain), use the OpenAI-compatible vLLM predictor runtime — it
proxies KServe's native protocol to OpenAI's API surface.

### Verifying the connection

```bash
# From inside the cluster:
oc exec -n virtvalidate deploy/virtvalidate-backend -- \
  curl -fsS https://granite.rhoai.svc.cluster.local/v1/models \
       -H "Authorization: Bearer $(cat /var/run/secrets/kubernetes.io/serviceaccount/token)"
```

The Settings page also surfaces the live KServe status under the **LLM
Backend** panel (`/api/system/llm-info` returns the resolved config and
a fresh health probe).

---

## Container images

VirtValidate ships two images:

| Image | Source | Default tag |
|-------|--------|-------------|
| `virtvalidate-backend`  | `backend/Containerfile`  | `0.1.0-alpha` |
| `virtvalidate-frontend` | `frontend/Containerfile` | `0.1.0-alpha` |

Both run as a non-root user with arbitrary-UID compatibility (group 0
file ownership) for OpenShift's `restricted-v2` SCC. The backend image
includes a `HEALTHCHECK` directive matching the Helm chart's
liveness probe.

### Building locally
```bash
# Default — both images, host architecture, current git SHA + version tag:
scripts/build-images.sh

# Push to a private registry:
scripts/build-images.sh -r registry.your-corp.com/virtvalidate -p

# Multi-arch build (requires Docker buildx or Podman manifest support):
scripts/build-images.sh -m -p
```

Produced tags:
- `<registry>/virtvalidate-<component>:<version>` (semver from `git describe`)
- `<registry>/virtvalidate-<component>:<git-sha>` (always)
- `<registry>/virtvalidate-<component>:latest` (push-only)

### CI builds
The `release-images.yml` GitHub Actions workflow does the same with
buildx, signs the images with cosign, and emits a CycloneDX SBOM.

---

## Choosing between Helm and Kustomize

Both produce equivalent Kubernetes resources. Pick based on workflow:

- **Helm:** value-driven config (e.g. `--set llm.kserve.endpoint=…` from CI), simpler upgrades (`helm upgrade --reuse-values`), better for fleet-style deployments where N clusters share one chart and N values files.
- **Kustomize:** GitOps-friendly (every overlay is a real YAML tree in version control), better for ArgoCD-managed environments where the diff between current state and desired state must be human-readable.

Some teams use both: a Helm chart at the source for upstream releases,
plus a Kustomize overlay applied via ArgoCD that vendors the rendered
Helm output. We don't recommend that for VirtValidate today — the
Kustomize tree we ship is hand-authored to match the Helm output, so
the chart and the overlays stay in sync without a render step.
