# VirtValidate

> AI-powered VM migration validation for VMware → OpenShift Virtualization

[![CI](https://github.com/cmalafis/virtvalidation/actions/workflows/ci.yml/badge.svg)](https://github.com/cmalafis/virtvalidation/actions/workflows/ci.yml)
[![CodeQL](https://github.com/cmalafis/virtvalidation/actions/workflows/codeql.yml/badge.svg)](https://github.com/cmalafis/virtvalidation/actions/workflows/codeql.yml)
[![Lighthouse a11y](https://github.com/cmalafis/virtvalidation/actions/workflows/lighthouse.yml/badge.svg)](https://github.com/cmalafis/virtvalidation/actions/workflows/lighthouse.yml)
[![Coverage](https://codecov.io/gh/cmalafis/virtvalidation/branch/main/graph/badge.svg)](https://codecov.io/gh/cmalafis/virtvalidation)
[![Latest release](https://img.shields.io/github/v/release/cmalafis/virtvalidation?include_prereleases&sort=semver)](https://github.com/cmalafis/virtvalidation/releases)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/downloads/release/python-3120/)
[![React 18](https://img.shields.io/badge/react-18-61dafb.svg)](https://react.dev/)

## What this is

VirtValidate is a self-hosted appliance that validates VMs migrated from
VMware to OpenShift Virtualization, then turns the validated waves into
ready-to-apply Migration Toolkit for Virtualization (MTV / Forklift)
plans. All reasoning runs on a local Ollama model — VMs, baselines, and
plans never leave the operator's network.

## Goals

- **Air-gapped by design.** No external API calls, ever. The platform
  ships with everything it needs to run in a regulated or classified
  environment.
- **Operator in the loop.** VirtValidate generates the wave plan and the
  MTV YAML; the operator reviews and applies. The appliance never
  touches the destination cluster directly.
- **Federal-friendly.** Apache 2.0 licensed, Podman-first, runs on
  RHEL/Fedora out of the box, and uses only OSS components.
- **Traceable.** Every mutating action lands in an audit log; every
  generated plan carries the rationale the LLM used to build it.

## Quickstart

```bash
# 1. Copy and configure environment
cp .env.example .env
$EDITOR .env

# 2. Generate the SSH key the appliance uses to reach VMs
mkdir -p backend/app/keys
ssh-keygen -t ed25519 -N "" -f backend/app/keys/id_ed25519 \
    -C "virtvalidate@appliance"

# 3. Pull Llama 3 (first run only)
podman-compose up -d ollama
podman-compose exec ollama ollama pull llama3:8b

# 4. Start everything
podman-compose up -d

# 5. Open the dashboard
open http://localhost:3000
```

## Documentation

| Doc | What it covers |
|-----|----------------|
| [docs/INSTALLATION.md](docs/INSTALLATION.md) | End-to-end Podman install, GPU setup, model pull, Quadlet for prod, troubleshooting |
| [docs/CONFIGURATION.md](docs/CONFIGURATION.md) | Every `.env` variable + which settings live in PostgreSQL and are editable from the UI |
| [docs/SSH_SETUP.md](docs/SSH_SETUP.md) | Key distribution playbook, sudoers rules, the exact commands the collector runs, rotation |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | System diagram, component breakdown, data flow, air-gap rationale |
| [docs/API.md](docs/API.md) | Reference for every HTTP endpoint, auto-generated from the FastAPI OpenAPI spec |
| [docs/vm-inventory-template.csv](docs/vm-inventory-template.csv) | CSV inventory template — paste into the dashboard's bulk upload tab |

API reference is auto-generated — re-run `python scripts/generate_api_docs.py`
after route changes. The live OpenAPI is also served at `/openapi.json` and
`/docs` when the backend is running.

## Requirements

- Podman 4.4+ (rootless)
- podman-compose 1.0.6+
- 16 GB RAM, 30 GB disk free
- NVIDIA GPU with CUDA 12+ recommended (CPU inference works, slower)
- RHEL 9 / Fedora 40+ recommended; Ubuntu 22.04+ also supported

## Architecture

```
┌─────────────────────────────────────┐
│  React Dashboard  :3000             │
├─────────────────────────────────────┤
│  FastAPI Backend  :8000             │
├──────────────┬──────────────────────┤
│  PostgreSQL  │   Ollama + Llama 3   │
│  :5432       │   :11434             │
└──────────────┴──────────────────────┘
All containers run rootless via Podman.
No data leaves this machine.
```

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the full system overview.

## VM inventory CSV format

[`docs/vm-inventory-template.csv`](docs/vm-inventory-template.csv) is the
canonical template for the bulk upload tab. Columns:

| Column | Required | Notes |
|--------|----------|-------|
| `hostname` | yes | DNS-resolvable source hostname (RVTools `DNS Name` works too) |
| `ip_address` | no | Primary IPv4 — used by the SSH collector |
| `ssh_username` | no | Account VirtValidate authenticates as on the source VM |
| `ssh_port` | no | Defaults to 22 |
| `current_platform` | no | Free-form (`vmware`, `vsphere-7`, …); informational |
| `role` | no | Hint to the planner (`database`, `app`, `loadbalancer`, `batch`, …) |
| `environment` | no | `prod` / `staging` / `dev`; informational |
| `owner` | no | Team or contact; informational |
| `vsphere_networks` | for MTV | Source portgroups, semicolon-separated |
| `vsphere_datastores` | for MTV | Source datastores, semicolon-separated |
| `target_namespace` | for MTV | Destination OpenShift namespace |
| `target_storage_class` | for MTV | Destination StorageClass |
| `target_network_attachment` | for MTV | Destination NetworkAttachmentDefinition (NAD) |
| `notes` | no | Free-form, surfaced in the dashboard |

Lists in `vsphere_networks` and `vsphere_datastores` go inside one CSV cell:

```csv
db-prod-01.corp.local,...,VM Network;DB Backend,nfs-prod-fast;nfs-prod-bulk,...
```

The MTV columns are optional for VMs you only want to *validate*, but they
are required for any VM you want to include in a generated MTV migration
plan (see below).

## Generating MTV migration plans

VirtValidate generates Migration Toolkit for Virtualization (MTV / Forklift)
plans straight from a wave. The flow is intentionally hands-off so the
operator stays in control of the actual cutover:

1. **VirtValidate generates the plan.** From the Migration Plan tab, click
   **↓ MTV YAML** on a wave. The dashboard calls
   `GET /api/plans/{plan_id}/waves/{wave_number}/mtv-yaml` and downloads a
   multi-document YAML containing one `NetworkMap`, one `StorageMap`, and
   one `Plan` (forklift.konveyor.io/v1beta1, warm migration by default).
   The maps are derived from the VMs' `vsphere_networks` /
   `vsphere_datastores` and their target `target_network_attachment` /
   `target_storage_class`.

2. **You review the YAML.** Open it in your editor. The Plan's
   `spec.description` carries the wave's planner rationale so reviewers
   know *why* these VMs are batched together. Adjust storage class names,
   NADs, or per-VM `namespace` overrides to match your cluster.

3. **You apply with `oc`.** From a workstation with cluster access:

   ```bash
   oc apply -f wave-1-plan-42.yaml
   ```

   MTV picks up the Plan and starts the migration. The Forklift controller
   reports progress back to the cluster — VirtValidate intentionally does
   not poll MTV state, so it keeps working in air-gapped environments
   where only the operator workstation has cluster credentials.

4. **You come back, mark the wave complete.** Once MTV finishes the wave,
   come back to VirtValidate to run post-migration validation and mark
   the wave done. The next wave's YAML is then ready to download.

The MTV provider names and namespace are configurable via
`MTV_NAMESPACE`, `MTV_SOURCE_PROVIDER`, `MTV_DESTINATION_PROVIDER`, and
`MTV_DEFAULT_TARGET_NAMESPACE` in `.env`. The Provider resources
themselves must already exist in the cluster — VirtValidate references
them but does not create them.

## Roadmap

Items below are loosely ordered. Anything ticked is in `main`; anything
unticked is fair game for a contribution — open an issue to discuss
scope before starting on a large item.

- [x] React dashboard with bulk CSV / RVTools XLSX enrollment
- [x] FastAPI backend skeleton, audit middleware, settings store
- [x] SSH baseline collector (Paramiko + Ed25519)
- [x] Local-LLM validation engine (Ollama + Llama 3)
- [x] LLM-powered migration wave planner with dependency awareness
- [x] PostgreSQL persistence with Alembic migrations
- [x] MTV / Forklift NetworkMap + StorageMap + Plan YAML generator
- [ ] One-click "Mark Wave Complete" with post-migration validation
- [ ] Wave-aware retry / partial re-validation flows
- [ ] OAuth / OIDC sign-in (currently anonymous behind reverse proxy)
- [ ] Quadlet bundle for systemd-managed prod deployments
- [ ] Pluggable LLM backend (vLLM, llama.cpp server) alongside Ollama
- [ ] CIS / STIG hardening profile presets for generated baselines

## Contributing

Contributions welcome — see [CONTRIBUTING.md](CONTRIBUTING.md) for dev
setup, code style (ruff + mypy), tests, and the DCO sign-off requirement.
Security issues should be reported privately per [SECURITY.md](SECURITY.md).

## License

Apache 2.0 — see [LICENSE](LICENSE).
