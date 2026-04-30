# Changelog

All notable changes to VirtValidate are documented in this file. Future
entries are generated automatically by [release-please](https://github.com/googleapis/release-please)
from [Conventional Commits](https://www.conventionalcommits.org/) on `main`.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0-alpha] — 2026-04-29

Initial alpha cut. Self-hosted, air-gapped VM migration validation
appliance for VMware → OpenShift Virtualization, with first-class
support for generating Migration Toolkit for Virtualization (MTV /
Forklift) plans straight from a wave.

### Features

- **React dashboard.** Multi-tab UI (validation, migration plan,
  inventory, reports, audit log) with sidebar quick actions, dark
  mission-control aesthetic, and bulk enrollment via CSV / RVTools
  XLSX. (`react dashboard, fastapi`, `vm front end updates`,
  `wire frontend changes`)
- **FastAPI backend skeleton.** Health, system, settings, audit,
  VMs, plans, and templates routers under `/api/*`. Audit middleware
  records every mutating call. (`react dashboard, fastapi`)
- **VM inventory + PostgreSQL persistence.** SQLAlchemy 2 models for
  VMs, baseline snapshots, validation results, plans, settings, and
  audit log. SQLite fallback for tests. (`vm inventory with db`)
- **SSH baseline collector.** Paramiko + Ed25519 key auth. Captures
  services, ports, mounts, network, DNS, cron. Keys live under
  `app/keys/`, never baked into images. (`baseline collection`)
- **Local-LLM validation engine.** Ollama-only inference. Pre/post
  state diff with reasoning, severity-tagged findings, and remediation
  steps. (`started llama`)
- **Migration wave planner.** LLM groups VMs into dependency-ordered
  waves with rationale and per-wave risk. Now also groups on shared
  vSphere networks and datastores. (`migration wave planner`,
  `network/storage mapping plus MTV migration plan yaml`)
- **MTV / Forklift YAML generator.** New `app/core/mtv.py` emits a
  multi-doc YAML (NetworkMap + StorageMap + Plan, warm migration
  default) per wave; new endpoint `GET /api/plans/{plan_id}/waves/
  {wave_number}/mtv-yaml` returns it as `application/yaml` with a
  one-click download in the UI. (`network/storage mapping plus MTV
  migration plan yaml`)
- **Wave reporter (PDF + JSON).** WeasyPrint-rendered report, audit
  trail entry on export. (`wave report generator`)
- **VM inventory CSV template.** Canonical
  `docs/vm-inventory-template.csv` plus `GET /api/templates/csv` and
  an in-modal "Download example CSV" entry point.
- **Alembic migration.** `0001_mtv_fields` adds vSphere networks/
  datastores + target namespace/storage class/network attachment.
  (`network/storage mapping plus MTV migration plan yaml`)
- **Settings + scheduler.** Single-row settings table, Ollama model
  picker, baseline collection cron preset (twice-daily / once-daily /
  hourly), live PostgreSQL + Ollama health probes. (`settings updates`)

### Bug Fixes

- API response format normalization across endpoints. (`api format fix`)
- Dashboard contrast issues fixed. (`fixed contrast issues`)

### Documentation

- Architecture, installation, configuration, SSH setup, and API
  reference under `docs/`. (`docs`)
- README rewritten with goals, badges, MTV plan-generation walkthrough,
  CSV column reference, and roadmap.
- CONTRIBUTING.md with development setup, testing, code style,
  Conventional Commits, DCO sign-off, and branch protection rules.
- Apache 2.0 LICENSE and SECURITY.md / CODE_OF_CONDUCT.md.
  (`licensing`)

### Continuous Integration

- ruff + mypy + pytest with 70% coverage gate. (`cicd tests`)
- Bandit, pip-audit, safety for Python security scanning.
- Trivy filesystem scan with CycloneDX SBOM artifact.
- Gitleaks secret scanning + pre-commit hook.
- ESLint with `eslint-plugin-security` + `npm audit` for the frontend.
- CodeQL SAST on Python and JavaScript/TypeScript, weekly + on PR.
- Lighthouse CI gating accessibility at ≥ 0.9 (WCAG-AA-friendly).
- Codecov upload + PR coverage comment.
- Dependabot for pip, npm, GitHub Actions, and Docker base images.
- CODEOWNERS routing all PRs to the maintainer.

### Styling

- Major readability + accessibility pass: bumped font sizes, switched
  body copy to Barlow (mono kept for technical data only), reduced
  uppercase letter-spacing, lifted contrast against `#07070f` to meet
  WCAG AA. (`ui fixes`)

[0.1.0-alpha]: https://github.com/cmalafis/virtvalidation/releases/tag/v0.1.0-alpha
