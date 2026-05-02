# Configuration

Every runtime setting is controlled by environment variables read from
`.env`. The compose file forwards them into the relevant containers.
Pydantic Settings (`app/core/config.py`) loads them in the backend; the
frontend reads `VITE_*` vars at build time.

A small subset is also editable at runtime via the **Settings** page
(`/settings`) — the dropdown there is the canonical control surface for
those values once the appliance is running.

---

## Backend (FastAPI)

### `DATABASE_URL`

| | |
|---|---|
| Default | `postgresql+psycopg2://virtvalidate:changeme@postgres:5432/virtvalidate` |
| Type | SQLAlchemy URL |
| Required | yes |

The connection string used by SQLAlchemy. Inside compose this reaches the
`postgres` service; for a managed RDS-style backend, point at the
external host.

```bash
# managed postgres
DATABASE_URL=postgresql+psycopg2://vv:hunter2@db.internal:5432/virtvalidate?sslmode=require
```

## LLM Backend Configuration

VirtValidate supports multiple LLM inference backends behind a single
abstract interface. The active backend is chosen at deployment time via
`LLM_BACKEND_TYPE` and **cannot** be switched at runtime — that's a
deployment decision, not an operator decision. The Settings page surfaces
the active backend read-only so operators can see what they're talking to.

### `LLM_BACKEND_TYPE`

| | |
|---|---|
| Default | `ollama` |
| Allowed | `ollama`, `kserve`, `vllm` |
| Editable in UI | no — deploy-time only |

Selects which inference engine the validation, planner, and network-review
flows route through. Pick based on your deployment infrastructure:

| Backend | When to use |
|---------|-------------|
| `ollama` | Air-gapped appliances, single-host deployments, dev/test |
| `kserve` | OpenShift with RHOAI, multi-tenant inference, model serving at scale |
| `vllm`   | Direct vLLM deployments (planned for v1.0.0 — placeholder today) |

---

### Ollama backend (default — standalone deployments)

Best for the air-gapped appliance shape that ships with `podman-compose`:
one Ollama container, one model, one validator.

```bash
LLM_BACKEND_TYPE=ollama
OLLAMA_HOST=http://ollama:11434
OLLAMA_MODEL=llama3:8b
```

#### `OLLAMA_HOST`

| | |
|---|---|
| Default | `http://ollama:11434` |
| Type | URL |

Where the validation engine, planner, and reporter post chat completions.
VirtValidate is air-gapped — point this at the local Ollama only.

#### `OLLAMA_MODEL`

| | |
|---|---|
| Default | `llama3:8b` |
| Editable in UI | yes — **Settings → LLM Model** |

Model tag passed to Ollama. Anything pulled into the local instance works
(`llama3:8b`, `llama3:70b`, `mistral:latest`, `qwen2.5:14b`, …). The
Settings page lists everything currently pulled and warns if the saved
model isn't loaded.

---

### KServe backend (RHOAI / OpenShift inference)

Best for OpenShift clusters with Red Hat OpenShift AI standing up
InferenceServices. VirtValidate talks to the OpenAI-compatible
`/v1/chat/completions` endpoint that vLLM and TGIS predictors expose.

```bash
LLM_BACKEND_TYPE=kserve
KSERVE_ENDPOINT=https://my-model.namespace.svc.cluster.local
KSERVE_MODEL_NAME=granite-3-8b-instruct
```

#### `KSERVE_ENDPOINT`

| | |
|---|---|
| Default | unset (required when `LLM_BACKEND_TYPE=kserve`) |
| Type | URL |

The InferenceService's predictor route. Internal cluster DNS works
(`*.svc.cluster.local`); external `*.apps.<cluster>.com` URLs work too
when VirtValidate runs outside the cluster.

#### `KSERVE_MODEL_NAME`

| | |
|---|---|
| Default | unset (required when `LLM_BACKEND_TYPE=kserve`) |

The model identifier vLLM/TGIS expects in the `model` field of the
chat-completions request. For RHOAI granite serving, this is typically
`granite-3-8b-instruct`.

#### Authentication

VirtValidate uses the in-pod service account token automatically when
deployed in OpenShift — `KSERVE_TOKEN_FILE` defaults to
`/var/run/secrets/kubernetes.io/serviceaccount/token`, which OpenShift
mounts on every pod. The token is re-read on every request so SA token
rotations apply without a restart.

For external KServe endpoints (off-cluster VirtValidate, dev clusters),
provide `KSERVE_TOKEN` explicitly:

```bash
KSERVE_TOKEN=eyJhbGciOiJSUzI1NiIs…
```

| Variable | Default | Notes |
|----------|---------|-------|
| `KSERVE_TOKEN` | unset | Bearer token; takes precedence over the file. |
| `KSERVE_TOKEN_FILE` | `/var/run/secrets/kubernetes.io/serviceaccount/token` | Path read fresh on every call. |
| `KSERVE_VERIFY_SSL` | `true` | Set to `false` only for dev clusters with self-signed certs. |
| `KSERVE_TIMEOUT_SECONDS` | `120` | Per-request timeout. |

When neither token nor token file resolves, the request goes out without
an `Authorization` header (only meaningful for dev clusters that disabled
auth).

---

### vLLM backend (planned — v1.0.0)

Direct vLLM connection without the KServe wrapper. Best for high-scale
concurrent inference where the KServe layer is overhead. Settings live
in `app/core/config.py` today so deployments don't need a config
migration when v1.0.0 lands; the implementation raises
`NotImplementedError`.

```bash
# Future:
LLM_BACKEND_TYPE=vllm
VLLM_ENDPOINT=https://vllm.namespace.svc.cluster.local
VLLM_MODEL_NAME=granite-3-8b-instruct
```

### `SSH_KEY_PATH`

| | |
|---|---|
| Default | `/app/keys/id_ed25519` |
| Type | absolute path inside the backend container |

Where the Ed25519 private key lives. The directory must be mounted into
the container (the compose file does this). The corresponding `.pub` file
is read for the **Settings → SSH Public Key** viewer; if it isn't on disk,
the public key is derived from the private one at request time and the
private key still never leaves the appliance.

### `CLUSTER_NAME`

| | |
|---|---|
| Default | `ocp-virt-prod-01` |
| Type | string |

Free-form label for the target OpenShift Virtualization cluster. Surfaces
in the UI header and in the PDF report footer.

### Postgres

| Variable | Default | Notes |
|----------|---------|-------|
| `POSTGRES_USER` | `virtvalidate` | DB role; matches the user in `DATABASE_URL` |
| `POSTGRES_PASSWORD` | `changeme` | **Change this before exposing the appliance.** |
| `POSTGRES_DB` | `virtvalidate` | Database name |

### `SECRET_KEY`

| | |
|---|---|
| Default | unset |
| Type | random hex string |

Reserved for signed tokens / session secrets in upcoming auth work.
Set it now to a long random value so the eventual auth roll-out doesn't
require a config change:

```bash
SECRET_KEY=$(python -c "import secrets; print(secrets.token_hex(32))")
```

---

## Frontend (Vite + React)

### `VITE_API_URL`

| | |
|---|---|
| Default | `http://localhost:8000` |
| Type | URL |

The base URL the frontend uses for `/api/*` calls. In dev, Vite's proxy
strips `/api` before forwarding (see `vite.config.js`). Override only if
you front the backend with a separate ingress.

---

## Settings managed at runtime, not in `.env`

Two values live in PostgreSQL (the `app_settings` singleton row) so they
can be changed at runtime without restarting any container:

| UI control | Field | Default |
|------------|-------|---------|
| **LLM Model** | `app_settings.ollama_model` | `llama3:8b` |
| **Baseline Schedule** | `app_settings.schedule_preset` | `twice_daily` |

The schedule preset accepts `twice_daily` (06:00 + 18:00 UTC), `once_daily`
(06:00 UTC), or `hourly`. Changes apply immediately — the running
APScheduler job is rescheduled on save.

---

## Reading the active configuration

The `/api/settings` endpoint returns the runtime-editable settings:

```bash
curl -s http://localhost:8000/api/settings | jq
```

The active LLM backend (type, model, endpoint, live health) is exposed
read-only at `/api/system/llm-info`:

```bash
curl -s http://localhost:8000/api/system/llm-info | jq
```

The static `.env` values aren't exposed via API by design (they include
secrets and tokens); inspect them with:

```bash
podman-compose exec backend env | grep -E '^(DATABASE_URL|OLLAMA_|KSERVE_|VLLM_|LLM_|CLUSTER_|SSH_)'
```
