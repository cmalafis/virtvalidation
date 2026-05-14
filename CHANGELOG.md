# Changelog

All notable changes to VirtValidate are documented in this file. Future
entries are generated automatically by [release-please](https://github.com/googleapis/release-please)
from [Conventional Commits](https://www.conventionalcommits.org/) on `main`.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.1-alpha](https://github.com/cmalafis/virtvalidation/compare/v0.1.0-alpha...v0.1.1-alpha) (2026-05-14)


### Features

* add windows OS support with validation checks ([5a99af7](https://github.com/cmalafis/virtvalidation/commit/5a99af7280d569b20c255101cc7bae0e1172a0c4))
* added creation issues ([a4d0390](https://github.com/cmalafis/virtvalidation/commit/a4d0390a25cf831342502034d5eb43b97e174e7b))
* FIPS 140-3 compliance support for federal deployments ([602defc](https://github.com/cmalafis/virtvalidation/commit/602defc5380afac8e94fbbfff25d5a34c1a9eb97))
* helm chart addition ([622372d](https://github.com/cmalafis/virtvalidation/commit/622372d9fa7eb4f3155ad929547eb83bfae8bf6a))
* migration planing ux initial design ([9908cb1](https://github.com/cmalafis/virtvalidation/commit/9908cb1eb4d00e0b79e0e630c6b90326e742365f))
* pluggable LLM backend abstraction with factory pattern ([8072745](https://github.com/cmalafis/virtvalidation/commit/80727454515a1fff98012236a8bc0530eb692076))
* scaling out migration planner ([e36063c](https://github.com/cmalafis/virtvalidation/commit/e36063c62cf21c3ec5a4ce03a63383d9b19a6bbc))
* ui fix for rvtools upload ([8746c68](https://github.com/cmalafis/virtvalidation/commit/8746c68c36686dcce22ced663ee5442168db29f5))


### Bug Fixes

* clean up lint warnings and stale type ignores ([daa0ecd](https://github.com/cmalafis/virtvalidation/commit/daa0ecdbedc49c56fe69faf25f25467c400270c7))
* **deps:** upgrade dependencies to address 7 CVEs ([eec1b36](https://github.com/cmalafis/virtvalidation/commit/eec1b362953ae17d084f1db2060b05a02b53c864))


### Documentation

* polished README and living product map ([bfef3c8](https://github.com/cmalafis/virtvalidation/commit/bfef3c88c2e6c6070ee6db7c3d996606589dd2ae))

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
