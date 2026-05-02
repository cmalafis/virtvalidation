# VirtValidate — One-Page Summary

> Snapshot 2026-05-02 — for AI-assistant context. See `docs/CODEBASE_SNAPSHOT.md` for the full version.

## What it is

Air-gapped appliance that validates VMs migrated from VMware → OpenShift
Virtualization. Captures pre-migration baselines via SSH, diffs against
post-migration state, and a local LLM reasons over the diff to produce
structured findings + remediation steps. Same LLM also generates
dependency-ordered migration waves and reviews proposed network designs.

## Stack

- **Backend:** Python 3.12, FastAPI, SQLAlchemy 2.0, APScheduler, Paramiko, WeasyPrint
- **Frontend:** React 18, Vite 6, react-router-dom, react-hot-toast
- **LLM:** Pluggable — `LLM_BACKEND_TYPE=ollama|kserve|vllm` (vLLM is a placeholder)
- **DB:** PostgreSQL 16 (SQLite in tests)
- **SSH transport:** Ed25519 / RSA-3072 / ECDSA P-384 keys; OpenSSH for Linux + Windows
- **Containers:** Podman / podman-compose for dev; Helm + Kustomize for OCP

## Architecture in one diagram

```
                   ┌─────────────────┐
                   │  React SPA      │  /sources/vcenters
                   │  (Vite)         │  /vms/:id
                   └────────┬────────┘  /design-reviews/*
                            │           /reports/:type
                            │           /settings  /
                            │
                ┌───────────▼────────────┐
                │   FastAPI              │  47 endpoints under /api
                │   AuditMiddleware      │  every mutating call → audit_logs
                └─┬───────────────┬──────┘
                  │               │
         ┌────────▼─────┐   ┌────▼─────────┐
         │  Postgres    │   │  LLM Backend │  Ollama (default) /
         │   16         │   │  Factory     │  KServe (RHOAI) /
         └──────────────┘   └────┬─────────┘  vLLM (placeholder)
                                 │
              ┌──────────────────┼──────────────────┐
              │                  │                  │
              ▼                  ▼                  ▼
       LLMClient          MigrationPlanner    NetworkReviewer
       (validation)       (wave grouping)    (design gap analysis)
              │
              ▼
       SSHCollector ──► Managed VMs (Linux + Windows Server)
       OS-aware: POSIX detection → Windows fallback
       Polymorphic key loader, FIPS gate, host-key TOFU
```

## Repo layout (top level)

```
virtvalidate/
├── backend/app/
│   ├── api/         # 11 routers (vms, plans, validations, vcenters, …)
│   ├── core/        # Domain logic — ssh, llm/, planner, fips, categorizer
│   ├── models/      # 8 SQLAlchemy models
│   ├── schemas/     # 8 Pydantic schema modules
│   ├── middleware/  # Audit middleware
│   └── main.py      # Lifespan + router registration
├── frontend/src/components/   # 7 components (one route each)
├── deploy/
│   ├── helm/virtvalidate/     # Chart + 13 templates
│   └── kustomize/             # Base + 4 overlays (standalone-ollama, rhoai-kserve, production, dev)
├── infra/quadlet/             # Production VM systemd units
├── docs/                      # 15+ docs (FIPS, SCALE, RVTOOLS, WINDOWS, …)
└── podman-compose.yml         # Dev environment
```

## Key data models (8)

| Model | Purpose |
|-------|---------|
| `VM` | Inventory row — also tracks status (`discovered`/`baseline_captured`/`migrated`/`validated`/`failed`), MTV mapping fields, vCenter scope, application_hint. |
| `BaselineSnapshot` | One row per SSH capture. `raw_data` JSON holds full state (services, ports, mounts, network, cron + Windows: hotfixes, ad_membership). |
| `ValidationResult` | LLM verdict — status + summary + findings (with severity/confidence/source_evidence/current_evidence/remediation per finding) + diff. |
| `MigrationPlan` | LLM-generated waves; one row per generated plan. |
| `VCenterSource` | Multi-vCenter scope with classification level (unclassified/cui/secret/top_secret). |
| `VMGroup` + `VMGroupMember` + `MigrationProgram` | Hierarchical planner output (Level 1 ships; L2/L3 placeholder). |
| `NetworkDesignReview` + `NetworkFinding` | Network gap analysis with triage. |
| `AppSettings` | Singleton runtime settings (model, schedule, host-key policy). |
| `AuditLog` | Federal-compliance audit trail — every mutating call. |

## API surface (47 endpoints, grouped)

- **VMs (16)** — CRUD + bulk + capture + validation + snapshots + baseline
- **Plans (6)** — create + list + per-wave PDF/JSON report + MTV YAML export
- **vCenters (9)** — CRUD + RVTools delta preview + Level 1 categorization + groups list
- **Validations (1 bulk)** — `POST /api/validations/run-all`
- **Reports (5)** — exec summary, baseline, full validation, wave plan, failed/degraded
- **Network reviews (7)** — CRUD + analyze + finding triage
- **Snapshots (1 bulk)** — `POST /api/snapshots/capture-all`
- **Health (4)** — full / llm / ollama (alias) / postgres
- **Settings + System (6)** — settings CRUD + ssh-public-key + ollama-models + llm-info + fips-status
- **Audit (1)** — filterable read
- **Templates (1)** — CSV download

## Test suite

**316 tests, all passing.** Hermetic — every test mocks the LLM
backend + SSH layer + httpx. Single test deselected in CI
(`test_csv_template_endpoint_returns_csv_attachment` — path resolution in container, not a code defect).

```
tests/test_api.py              — basic API smoke
tests/test_capture.py          — capture flow + bulk delete + audit diff
tests/test_categorizer.py      — Level 1 batched categorizer (16 tests)
tests/test_fips.py             — FIPS 140-3 enforcement (30 tests)
tests/test_host_keys.py        — TOFU + strict mode + audit
tests/test_llm.py              — diff helpers + verdict parsing + stub-backend e2e
tests/test_llm_backends.py     — Ollama + KServe + vLLM + factory (26 tests)
tests/test_mtv.py              — MTV YAML generation
tests/test_network_review.py   — Reviewer parser + analyze flow
tests/test_os_profile.py       — OS detection + dispatch + Windows path (39 tests)
tests/test_planner.py          — Wave plan parser + grouping rules
tests/test_reporter.py         — Wave PDF report
tests/test_reports.py          — 5 dashboard report endpoints
tests/test_settings.py         — Settings + health + system endpoints
tests/test_ssh.py              — SSH collection (mocked)
tests/test_validation_api.py   — Validation API (15 tests)
tests/test_vcenters.py         — vCenter CRUD + RVTools delta (16 tests)
```

## What shipped this past week (5 feat commits)

1. **Pluggable LLM backend** — `app/core/llm/` package with ABC, factory, Ollama/KServe/vLLM impls. Settings page surfaces the active backend read-only.
2. **FIPS 140-3 compliance** — `core/fips.py` with OS detection, SSH key gate (rejects Ed25519 in fips_mode), `/api/system/fips-status` endpoint, Settings UI panel, deployment guide.
3. **Helm + Kustomize** — `deploy/helm/virtvalidate/` chart (lint-clean) + 4 Kustomize overlays. Hardened Containerfiles for OCP `restricted-v2` SCC. `scripts/build-images.sh`.
4. **Windows Server SSH** — extends existing OS-aware foundation. PowerShell + `ConvertTo-Json` command set, BOM-aware `_run`, normalizes Windows output to the same Linux-shaped raw_data so the diff engine + LLM prompt don't fork. `docs/WINDOWS_SETUP.md`.
5. **Scale-aware migration planning** — `VCenterSource` registry, RVTools delta preview, Level 1 batched LLM categorizer (`core/categorizer.py`). UI at `/sources/vcenters`. Levels 2/3 deferred with explicit notes in `docs/SCALE.md`.

## Known gaps (deferred, all documented in `docs/SCALE.md`)

- **Hierarchical planner Levels 2 + 3** — L1 ships; L2 (strategy) + L3 (per-campaign waves) need streaming UI work.
- **UI virtualization** — inventory works ≤1,000 rows; `react-window` upgrade for larger fleets.
- **Multi-program switcher UI** — data model supports it, UI is single-context.
- **Real-time progressive streaming** — tasks poll every 3-4s; SSE upgrade tagged.
- **Alembic migrations** — currently `Base.metadata.create_all`; production schema changes need a fresh volume.
- **vLLM direct backend** — placeholder; KServe with vLLM predictor satisfies most cases.
- **Real Windows VM smoke test** — fixtures realistic, first real-cluster validation pending.
- **Frontend test coverage** — no Playwright/Cypress; Lighthouse a11y only.

## Configuration cheat sheet

```bash
# Standalone (default)
LLM_BACKEND_TYPE=ollama
OLLAMA_HOST=http://ollama:11434
OLLAMA_MODEL=llama3:8b

# RHOAI / OpenShift
LLM_BACKEND_TYPE=kserve
KSERVE_ENDPOINT=https://granite.ns.svc.cluster.local
KSERVE_MODEL_NAME=granite-3-8b-instruct
# KSERVE_TOKEN_FILE defaults to in-pod SA token mount

# Federal deployments
FIPS_MODE=true
SSH_KEY_ALGORITHM=rsa-3072  # or ecdsa-p384
```

Runtime settings (Settings page → DB):
- `ollama_model` (dropdown)
- `schedule_preset` (twice_daily / once_daily / hourly)
- `ssh_host_key_policy` (auto_accept / strict)

Helm chart values (`deploy/helm/virtvalidate/values.yaml`) cover everything else — image tags, replicas, resources, llm.*, security.fipsMode, networkPolicies.enabled, route.enabled, etc.

## Recent deployment paths

| Path | Best for | Quick start |
|------|----------|-------------|
| podman-compose | Dev / single-host / demo | `podman-compose up -d` |
| Helm + Ollama | Single OCP cluster | `helm install virtvalidate deploy/helm/virtvalidate/` |
| Helm + KServe | RHOAI / federal | `--set llm.backend=kserve --set llm.kserve.endpoint=…` |
| Kustomize | ArgoCD GitOps | `kubectl apply -k deploy/kustomize/overlays/standalone-ollama` |

See `deploy/README.md` for the full guide.

## Things to know when picking up the codebase

- **`Base.metadata.create_all`** runs at startup. SQLite drops + recreates per test; Postgres in dev needs a fresh volume on schema changes.
- **BackgroundTasks** drive every long-running job (capture, validation, categorization). Each has its own `task_store` (in-memory, RLock-guarded) for status polling. Tasks evaporate on process restart by design — UIs surface "task not found, refresh inventory" gracefully.
- **`LLMClient`/`MigrationPlanner`/`NetworkReviewer`/`Categorizer`** all take an `LLMBackend` in their constructor (DI). Default to `get_llm_backend()` for production; tests inject stubs.
- **Audit middleware** auto-logs every mutating HTTP call. Endpoints emitting richer custom audits set `request.state.skip_audit_log = True` to avoid double-logging.
- **`request.state` flags** are how the audit middleware coordinates with handlers — grep for `skip_audit_log` to see the convention.
- **Inline styles only** in the frontend — no Tailwind / styled-components. Each component re-defines its own `fetchJSON` helper (minor duplication, no shared API client today).
- **SSH collector is OS-agnostic** — same `raw_data` shape regardless of Linux vs Windows. The Windows path normalizes JSON output to match the Linux line-parsed shape.
- **CLAUDE.md** at repo root is the AI-assistant instruction file — read it at session start.
