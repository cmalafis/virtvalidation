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

### `OLLAMA_HOST`

| | |
|---|---|
| Default | `http://ollama:11434` |
| Type | URL |

Where the validation engine, planner, and reporter post chat completions.
Must be the local Ollama instance — VirtValidate is air-gapped and refuses
to call external LLM APIs by design.

### `OLLAMA_MODEL`

| | |
|---|---|
| Default | `llama3:8b` |
| Editable in UI | yes — **Settings → LLM Model** |

Model tag passed to Ollama. Anything pulled into the local instance works
(`llama3:8b`, `llama3:70b`, `mistral:latest`, `qwen2.5:14b`, …). The
Settings page lists everything currently pulled and warns if the saved
model isn't loaded.

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

The static `.env` values aren't exposed via API by design (they include
secrets); inspect them with:

```bash
podman-compose exec backend env | grep -E '^(DATABASE_URL|OLLAMA_|CLUSTER_|SSH_)'
```
