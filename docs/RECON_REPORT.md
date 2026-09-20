# VirtValidate Reconnaissance Report

_Generated 2026-06-02 after a ~2 week absence. Read-only assessment — no code, schema, config, or docs were modified; nothing committed._

## Executive Summary

The project is in **substantially better shape than a two-week gap usually leaves it**. Every major workstream that was in flight before the absence has landed in `main` and is merged cleanly: the multi-cluster target-architecture refactor (PR #10), the deterministic planner pipeline + VM lifecycle + 1000-VM cap (PR #7), the dual-variant image / Snyk / Trivy security hardening (PR #12), the wave-scoped baseline + validation engine (PR #13), and the MaaS LLM backend with runtime backend switching (PR #14). The working tree is clean, `main` is up to date with origin, three release tags are applied (`v0.2.0/.1/.2`), and CLAUDE.md's architectural claims hold up almost exactly against the code — the one nuance is cosmetic (field naming in the partition key). The test suite is healthy: **990 backend tests pass at 82% coverage** on isolated in-memory SQLite. There is essentially **no loose-end debt** — no stray TODOs, no skipped tests, no orphaned components, one documented `vllm` placeholder by design.

The risks are narrow and concrete. **CI is red on `main` for three independent reasons, only one of which is real code debt**: 15 auto-fixable `ruff` violations were merged without running the formatter (the lint gate is genuinely failing); the Snyk job fails on a missing `SNYK_TOKEN` secret plus an unguarded SARIF upload; the image-scan job fails on a GitHub Advanced Security SARIF-upload permission (the images themselves build clean and Trivy reports zero vulnerabilities). The single most important forward-looking signal: **the LLM client does not persist inference inputs/outputs** — only token/latency telemetry, and wave-annotation calls aren't even captured there — so the planned TrustyAI integration would be a retrofit unless that gap is closed first. Finally, the deploy path is **clean and cluster-agnostic**: no dead-cluster references survive anywhere in the repo, the Helm chart lints and renders, and getting onto the new sandbox cluster with MaaS is a standard `helm install` with a handful of `--set` overrides (documented below). The recommended first action is the cheapest high-leverage one: **get CI green** so the next feature session starts from a trustworthy baseline.

## What's Landed in Main

All of the following are merged and present on `main` (HEAD `0c9395e`):

- **Multi-cluster target architecture (PR #10, `ad83533`…`23f9de5`)** — per-VM `target_cluster_id_override` / `target_namespace_override`, partition by the 4-tuple, mapping uniqueness per `(vcenter, target)` pair, `is_active` removed, OCP target namespaces CRUD, MTV YAML provider scoping + lowercase VM names, selection cap raised to 1000.
- **Deterministic planner rearchitecture (PR #7, `99cbe77`…`e12cf76`)** — Stages 0–5 + 7 pure-Python, Stage 6 parallel per-wave LLM annotation with retry/fallback, VM plan-membership lifecycle, selection cap + 422, mark-succeeded, YAML download.
- **Dual-variant images + Snyk/Trivy CI (PR #12, `3dbebe2`)** — UBI digest pins, `Containerfile.hardened` variants (`ARG BASE_IMAGE`), `scripts/build-all-images.sh`, Helm `image.variant` toggle, Snyk SAST/SCA + Trivy image scan, `docs/SECURITY_POSTURE.md`, weasyprint→xhtml2pdf swap, Project Hummingbird hardened registry.
- **Wave-scoped baseline + validation engine (PR #13, `61f611a`)** — `ssh_keys`/`baseline_runs`/`baselines`/`validation_runs`/`vm_validations` tables, full `app/core/collection/` module, bounded SSH concurrency, Ansible setup/teardown playbooks, wave-scoped baseline/validate/revoke-key endpoints, `WaveRunsPanel.jsx` frontend.
- **MaaS LLM backend + runtime switching (PR #14, `0c9395e`)** — all 5 backends in the enum, DB-backed active-backend setting with TTL cache, bearer-auth OpenAI-compatible MaaS client, test-connection endpoint, auth-failure surfacing to `last_llm_error`.
- **Earlier polish/fix PRs (#6, #8, #9)** — mapping editor UX + FK-protected deletes, multi-select mappings, wave hostnames, lifecycle filters, YAML 422 detail.

CI simplification (PR #11) dropped speculative scanners down to gitleaks-only pre-commit before the security PRs re-added Snyk/Trivy in CI.

## Work In Flight (Branches)

| Branch | State | Assessment | Recommendation |
|---|---|---|---|
| `feat/dual-variant-images-snyk` | 12 ahead / 3 behind `main` | **Squash-merge artifact, not lost work.** PR #12 squash-merged this branch as `3dbebe2`; git can't see the squashed commit as a descendant, so the 12 originals still read as "ahead." Every artifact (build-all-images.sh, Containerfile.hardened, SECURITY_POSTURE.md, Snyk/Trivy workflows, image.variant, xhtml2pdf swap, Hummingbird registry) is on `main` **byte-for-byte identical**. The 10k-line "deletions" in the 3-dot diff are `main` moving forward (PRs #13/#14 deleted/rewrote frontend files the stale branch predates). | **Safe to delete** (`git push origin --delete feat/dual-variant-images-snyk`). Nothing unmerged. |
| `release-please--branches--main` | 1 ahead / 4 behind, PR #4 OPEN | The release-please bot's standing "chore: release main" PR. Accumulates changelog entries + version bumps; meant to stay open and is force-updated each push. | Normal. Merge it when you want to cut a release; otherwise leave it. |

Local-only branches `feat/dual-variant-images-snyk` and `fix/mapping-editor-500` are stale checkouts of already-merged work. The remote `ci-simplification`, `feat/baseline-validation-engine`, and `feature/maas-llm-backend` branches were already pruned server-side (deleted during `git fetch --prune`).

## Discrepancies: CLAUDE.md vs Code

| CLAUDE.md claim | Actual code state | Severity |
|---|---|---|
| Plans partition by `(source_vcenter, target_cluster, target_namespace, environment)` | `preclassifier.py:635-640` uses `(vm.source_vcenter_id, vm.target_cluster_id_override, target_namespace_override.strip(), env.value)` — semantically identical, field names use the `*_override` columns and normalized `env.value`. | Cosmetic |
| VM has nullable `target_cluster_id_override` + `target_namespace_override` | Holds — `models/vm.py:102-107`. | — |
| Mappings unique per `(vcenter_source_id, ocp_target_id)` | Holds — `models/target.py:165-178`, constraint `uq_mapping_per_pair`. | — |
| `is_active` removed from mappings | Holds — dropped in migration `6e216aa1ffcf`; zero references in `app/`. | — |
| Selection cap 1000, enforced with 422 | Holds — `config.py:114` (`max_vms_per_plan: int = 1000`), enforced `api/plans.py:156-164`. | — |
| OCPTargetNamespace model + CRUD | Holds — `models/ocp_namespace.py`, 5 endpoints in `api/target_entities.py`. | — |
| MTV YAML = Plan + NetworkMap + StorageMap, lowercase VM names | Holds — `core/mtv.py:367-498`; lowercasing via `_rfc1123_name`→`_slugify` (`mtv.py:146`). | — |
| 5 LLM backends (mock/ollama/kserve/vllm/maas) | Holds — `core/llm/types.py:20-25`. | — |
| Active backend DB-backed (`AppSettings.active_llm_backend`) w/ TTL cache | Holds — `models/settings.py:59-68`, `core/llm/runtime.py:97-113` (60s TTL). | — |
| MaaS key from `LLM_MAAS_API_KEY`, never logged/in exceptions | Holds — `core/llm/maas_backend.py` redacts to `bearer:****`, scrubs error messages; key sourced via secretKeyRef in Helm. | — |
| Auth failure → `last_llm_error` + `method="mechanical_fallback_auth"` | Holds — `core/llm/status.py:52-77`, `core/wave_annotation.py:453-479`. | — |
| `vllm` backend is a placeholder raising NotImplementedError | Holds — `core/llm/vllm_backend.py:54,59`, documented. | — |

**Net: CLAUDE.md is an accurate description of the code.** The only divergence is the partition-key field naming, which is a documentation nicety, not a functional gap.

## Half-Implementations & TODOs

The codebase is unusually clean. Specific findings:

- **`vllm` backend stub** — `core/llm/vllm_backend.py:54,59` raise `NotImplementedError`. Intentional, documented in CLAUDE.md; the class exists so the factory dispatch + config validation accept `vllm` without a future migration. _Not_ debt.
- **Campaign/hierarchical-planner tables** — `models/grouping.py:14,132` comment Level-2/Level-3 tables as "placeholders … full hierarchical planner is in flight." Tables exist; feature not built out. Known future work, not broken.
- **No actionable TODO/FIXME/XXX/HACK** in `backend/app` or `frontend/src` (only doc-comment mentions).
- **No skipped/xfail tests** — every "skip" match is pagination vocabulary or RVTools "skipped rows," not disabled tests.
- **No frontend orphans** — legacy `GeneratePlanModal` is confirmed gone; `PlanWizard` is the single live plan-creation component. All 18 API modules' routers are `include_router`'d in `main.py`. All 20+ frontend routes map to real components.
- The `mtv.py:390` / `plan_pipeline.py:187,294` / `wave_annotation.py:550` "placeholder" hits are guard-rails that _refuse_ to emit placeholder names (the federal "no placeholder names" rule) — the opposite of stubs.

## Test & CI Health

**Tests (healthy):** 58 backend test files, **990 tests, all passing, 82% branch coverage** (ran in 153s). DB isolation is correct — `conftest.py:14` forces `sqlite:///:memory:` with `StaticPool` before app import and rebinds `SessionLocal`; no real Postgres is reachable, so the run was safe. Coverage spread is good; the notable low spot is `services/ssh_key_service.py` at **56%** (day-2 key-revocation paths under-tested). **Frontend has zero tests** — no `test` script, no vitest/jest, no `*.test.*` files; correctness is guarded only by ESLint.

**CI (red — three distinct causes, one real):**

1. **`CI` workflow — REAL code debt.** The `ruff + pytest` job fails at the **ruff lint** step (pytest never runs). 15 errors, all `[*]` auto-fixable: F401 unused imports (`services/ssh_key_service.py:31`, `core/collection/wave_jobs.py:26`, `tests/test_baseline_run_flow.py` ×6, …) and I001 unsorted import blocks (`api/ssh_keys.py:12`, `core/collection/collector_spec.py:35`, `core/reporter.py:530`). Root cause: code merged without `ruff check --fix . && ruff format .`. **This means `main` currently violates its own lint gate.**
2. **`Snyk security scan` — infra.** `SNYK_TOKEN` renders empty (secret absent), so the Snyk Python step errors and produces no SARIF; the `if: always()` SARIF-upload step then hard-fails on the missing file. Two fixes: add the secret, and add the `hashFiles()` guard the Code-SARIF step already has (`snyk.yml:47-52,65-70` are unguarded).
3. **`Build & scan images` — infra.** Images build fine and Trivy reports **0 vulnerabilities**; the failure is the `codeql-action/upload-sarif` step: "Resource not accessible by integration" — GitHub code scanning / GHAS isn't enabled (or guarded) for this repo. Fix: enable GHAS or guard/skip the upload.

## Security Posture Status

Strong and federal-aligned:

- **Base images:** all standard Containerfiles digest-pinned to `registry.access.redhat.com` UBI 9 (no Docker Hub, no `:latest`). Hardened variants (`backend/Containerfile.hardened`, `frontend/Containerfile.runtime.hardened`) parameterized via `ARG BASE_IMAGE` for RHHI/Project Hummingbird. `scripts/build-all-images.sh` builds the matrix; Helm `image.variant` toggle wired end-to-end (`values.yaml:22`, `_helpers.tpl:81`).
- **Scanners:** gitleaks (CI job + pre-commit, blocking), Snyk SAST+SCA (`snyk.yml`, weekly + push/PR), Trivy image scan standard+hardened (`images.yml`). CodeQL intentionally omitted (Snyk Code covers SAST). Dependabot across 6 ecosystems. `docs/SECURITY_POSTURE.md` present (11.8 KB).
- **MaaS secret handling:** API key only via `LLM_MAAS_API_KEY` from a K8s Secret (`secretKeyRef`, never in values.yaml); redacted in `__repr__`, health checks, and scrubbed from error messages before being stored in `last_llm_error`.
- **Caveats:** the two CI infra failures above are posture-adjacent (SNYK_TOKEN, GHAS) but do not reflect insecure code. Snyk/Trivy steps are deliberately `continue-on-error` ("Phase 1 surface-only") — findings are surfaced, not yet gating.

## Local-Deploy Readiness

**No dead-cluster references anywhere.** Grep across `deploy/`, `scripts/`, `docs/` finds only illustrative example hosts (e.g. `litellm.example.com`, `virtvalidate.apps.your-cluster.com` placeholders). The repo was never pinned to the old cluster; the chart is cluster-agnostic — `storageClass: ""` (cluster default), `route.host: ""` (OpenShift auto-assigns from the wildcard domain). `scripts/verify_mappings.sh` uses a throwaway SQLite + fake URLs; nothing to un-wire.

**Local (podman-compose):** `podman-compose.yml` at repo root (there is no `deploy/local/`). Brings up frontend, backend, postgres (`docker.io/postgres:16-alpine`), ollama. `LLM_BACKEND_TYPE=mock` is hardcoded (`:39`) — correct per the dev-infra decision. Requires a root `.env` (compose references `${DATABASE_URL}`/`${SECRET_KEY}` with no defaults); `.env.example` is the template. Migrations auto-run at startup (`main.py:122`).

**Helm:** chart `version 0.1.4`, `appVersion 0.1.2-alpha`; `helm lint` passes, `helm template` renders clean (zero hardcoded storageClass/host/old-domain hits). MaaS wiring present (`values.yaml:190-208`). Migrations run two ways — startup `apply_migrations` + a `pre-install,pre-upgrade` Helm Job — so **no manual seed**. Resource requests are sandbox-sane except **Ollama (req 4Gi / lim 16Gi / 20Gi PVC)**, which default-deploys regardless of backend — must be disabled when using MaaS.

### First-deploy checklist — sandbox + MaaS

```bash
# 0. Pull + log in
git pull
oc login https://api.ocp.example.opentlc.com:6443

# 1. Namespace
oc new-project virtvalidate

# 2. MaaS API-key Secret  (key name MUST be 'api-key' to match existingSecretKey default)
oc create secret generic virtvalidate-maas \
  --from-literal=api-key='<YOUR_MAAS_KEY>' -n virtvalidate

# 3. Install  (route.host + storageClass intentionally omitted → cluster defaults)
helm install virtvalidate deploy/helm/virtvalidate/ -n virtvalidate \
  --set llm.backend=maas \
  --set llm.maas.enabled=true \
  --set llm.maas.baseUrl='https://<MAAS_HOST>.apps.ocp.example.opentlc.com/v1' \
  --set llm.maas.model='<MODEL_NAME>' \
  --set llm.maas.existingSecret=virtvalidate-maas \
  --set llm.ollama.deploy=false \
  --set route.enabled=true

# 4. Watch migration Job + rollout, then get the route
oc get jobs,pods -n virtvalidate -w
oc get route virtvalidate -n virtvalidate -o jsonpath='{.spec.host}{"\n"}'
```

Notes: `llm.maas.baseUrl` **must include `/v1`**; `llm.maas.model` is **required** when MaaS is enabled (template fails otherwise). `--set llm.ollama.deploy=false` is essential or you provision a 16Gi/20Gi Ollama pod you don't need. App images are `quay.io/cmalafis10/virtvalidate-{backend,frontend}:0.1.2-alpha` — confirm those tags are pushed/public. After install, mark MaaS active in the Settings UI (Test connection first); the `--set llm.backend=maas` is only the bootstrap seed — the DB row is authoritative thereafter.

### RHOAI / MaaS / TrustyAI readiness

- **MaaS backend is complete** — bearer auth, secret-sourced key, health check, test-connection endpoint. It has everything needed to point at the sandbox MaaS endpoint; no gaps.
- **No conflicting in-cluster assumption.** The `kserve` backend assumes an in-cluster InferenceService (SA-token from the mounted service-account path); **MaaS is fully independent of it** — choosing MaaS does not require KServe. RHOAI being present is irrelevant to the MaaS path.
- **TrustyAI gap (the key forward signal): inference inputs/outputs are NOT persisted.** `LLMUsage` (`models/llm_usage.py`) stores only telemetry — operation, backend, model, token counts, latency, optional resource linkage. Validation calls record this; **wave-annotation calls record nothing at all** (only the final `method` field on the wave). Prompts, responses, and model decisions are ephemeral. This is fine for "which fallback path fired and why" audit, but **insufficient for TrustyAI explainability**, which needs the actual input the model saw and the output it produced per call. Closing this (a structured per-call inference record) before the TrustyAI session turns that integration into a clean add rather than a retrofit.

## Recommended Next-Session Backlog

Prioritized. Session weight in parentheses; dependencies noted.

1. **★ TOP — Get CI green (small).** Run `ruff check --fix . && ruff format .` in `backend/` (fixes the 15 lint errors gating the real CI job); add `SNYK_TOKEN` as a repo secret _or_ add the `hashFiles()` guard to the unguarded Snyk SARIF-upload steps; enable GHAS code scanning _or_ guard/skip the Trivy SARIF upload. _Why first:_ cheapest high-leverage item — `main` currently fails its own lint gate, and a red board hides real regressions in every future PR. No dependencies. **Do this before anything else.**
2. **TrustyAI prerequisite — persist structured inference records (medium).** Add a per-call inference log (input, output, model, latency, fallback-fired) covering wave annotation _and_ validation, written through the LLM client. _Why:_ the single most valuable forward investment; makes the eventual TrustyAI work a clean add. Depends on nothing; best done before the TrustyAI feature session.
3. **First real sandbox deploy + smoke test (medium).** Execute the checklist above, point at MaaS (Granite via LiteLLM), validate end-to-end (import → plan → baseline → validate). _Why:_ proves the cluster-agnostic chart against the new environment; surfaces any MaaS/route/storageClass reality the static read can't. Depends loosely on #1 (clean images) and a real MaaS endpoint/key.
4. **Prune stale branch + tidy release (small).** Delete `feat/dual-variant-images-snyk` (fully merged); decide whether to merge release-please PR #4 to cut a tagged release. _Why:_ removes the misleading "12 ahead" signal. No dependencies.
5. **Frontend test harness (medium).** Stand up Vitest + React Testing Library; cover `WaveRunsPanel`, `PlanWizard`, and the defensive-null contracts CLAUDE.md mandates. _Why:_ the only correctness gap in an otherwise well-tested codebase; frontend currently has zero tests.
6. **Backfill `ssh_key_service` coverage (small).** Cover the 56%-covered day-2 revocation paths (`services/ssh_key_service.py:248-337`). _Why:_ security-sensitive code (key removal from VMs) is the least-tested module. Depends on nothing.
7. **Hierarchical-planner campaign tables (large).** Build out the Level-2/Level-3 grouping placeholders in `models/grouping.py`. _Why:_ the one genuinely unfinished feature area. Largest weight; only when the roadmap calls for it.

## Open Questions for the Operator

- **MaaS endpoint specifics:** what is the actual LiteLLM base URL and model name on sandbox, and is the API key already issued? The checklist needs these three values.
- **Release cadence:** should release-please PR #4 be merged to cut `0.1.1-alpha`, or is the project staying on the `v0.2.x` manual tags? The two version schemes (`Chart.appVersion 0.1.2-alpha` vs git tags `v0.2.2-fixes`) are diverging.
- **CI gating intent:** should Snyk/Trivy stay `continue-on-error` ("surface-only"), or is this the session to make them blocking? Affects whether fix #1 just unblocks the board or tightens the gate.
- **TrustyAI scope:** is the goal full prompt/response capture (heavier storage, PII considerations on VM data) or decision-level metadata only? This shapes backlog item #2's schema.
- **vllm backend:** still a placeholder by design, or is direct-vLLM now on the roadmap given RHOAI is installed? (Decision recorded as MaaS-not-KServe, but vllm is a third option.)

## Appendix: Raw Findings (file:line)

- Partition key: `backend/app/core/preclassifier.py:635-640`
- VM overrides: `backend/app/models/vm.py:102-107`
- Mapping uniqueness: `backend/app/models/target.py:165-178`; `is_active` drop: `migrations/versions/20260513_6e216aa1ffcf_*.py:233`
- Selection cap: `config.py:114`; enforcement `api/plans.py:156-164`
- Namespaces: `models/ocp_namespace.py:35-60`; CRUD `api/target_entities.py:495-550`
- MTV emit/lowercase: `core/mtv.py:367-498`, `:146`, `:335-338`
- LLM enum: `core/llm/types.py:20-25`; runtime cache `core/llm/runtime.py:97-113`; factory `core/llm/factory.py:41-56`
- MaaS client: `core/llm/maas_backend.py:42-94,129-134`; test-connection `api/settings.py:212-236`, `runtime.py:206-286`
- Auth surfacing: `core/llm/status.py:52-77`; `core/wave_annotation.py:453-479`
- KServe in-cluster auth: `core/llm/kserve_backend.py:11,76-91`
- TrustyAI gap: `models/llm_usage.py` (telemetry only); `core/wave_annotation.py:403-518` (no per-call record); `core/validation.py` (`_record_llm_usage`, telemetry only)
- SSH keys: `models/ssh_key.py:31-75`; schema safety `schemas/ssh_key.py:26-41`
- Baseline tables: `models/baseline_run.py:61-159`; validation `models/validation_run.py:61-151`
- Collection module: `core/collection/{__init__,collector_spec,engine,orchestrator,diff,wave_jobs}.py`
- SSH concurrency: `config.py:18` (`ssh_max_concurrency: int = 25`)
- Probe fields: `models/baseline_run.py:138-139`
- Ansible: `deploy/ansible/setup-virtvalidate-user.yml`, `remove-virtvalidate-user.yml`
- Wave endpoints: `api/waves.py:141-202` (baseline), `:332-412` (validate), `:452-549` (revoke-key)
- Frontend wave UI: `frontend/src/components/WaveRunsPanel.jsx`
- vllm stub: `core/llm/vllm_backend.py:54,59`; campaign placeholders `models/grouping.py:14,132`
- CI failures: `.github/workflows/ci.yml` (ruff), `snyk.yml:47-52,65-70` (unguarded SARIF), `images.yml:24` (SARIF upload perms)
- Containerfiles: `backend/Containerfile:6`, `frontend/Containerfile:7,19`, `*.hardened`; `scripts/build-all-images.sh`
- Helm: `Chart.yaml` (0.1.4 / 0.1.2-alpha), `values.yaml:22,190-208,241`, `_helpers.tpl:81,190-208`, `templates/{route-frontend,job-migrations,deployment-ollama}.yaml`
- Compose: `podman-compose.yml:39` (mock default)
- Tests: `conftest.py:14` (in-memory SQLite); 990 tests / 82% cov; low spot `services/ssh_key_service.py` 56%
- Branch reconciliation: `git diff main...origin/feat/dual-variant-images-snyk` (squash-merge artifact; all artifacts identical on main)
