<div align="center">

<img src="docs/assets/logo.svg" alt="VirtValidate" width="120" />

# VirtValidate

**Migration planning and validation for VMware → OpenShift Virtualization**

Turn an RVTools export into dependency-aware migration waves with MTV-ready YAML, adjust them by hand, and optionally validate every VM after it moves. Runs air-gapped, with AI inference you host yourself.

[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![Release](https://img.shields.io/github/v/release/cmalafis/virtvalidation?include_prereleases)](https://github.com/cmalafis/virtvalidation/releases)
[![Python](https://img.shields.io/badge/python-3.12-3776AB?logo=python&logoColor=white)](https://www.python.org)
[![Podman](https://img.shields.io/badge/podman-rootless-892CA0?logo=podman&logoColor=white)](https://podman.io)
[![UBI 9](https://img.shields.io/badge/base-Red%20Hat%20UBI%209-EE0000?logo=redhat&logoColor=white)](docs/CONTAINER_IMAGES.md)

[Quick Start](#quick-start) · [Planning Quickstart](docs/PLANNING_QUICKSTART.md) · [Product Map](docs/product-map.html) · [Architecture](docs/architecture-diagram.html) · [Roadmap](https://github.com/cmalafis/virtvalidation/projects) · [Contributing](CONTRIBUTING.md)

</div>

---

## Why VirtValidate

VMware-to-OpenShift Virtualization migrations involve hundreds or thousands of VMs and almost no tooling for the most critical question: **did each VM actually migrate correctly?**

Existing tools handle the mechanics of migration — moving disks, recreating VMs, mapping networks. None of them tell you whether the migrated VM is actually healthy, configured the same as before, and ready for production traffic.

VirtValidate covers both ends:

- **Planning (no VM access needed).** Import RVTools, map networks / datastores / namespaces to the target cluster, and get every VM packed into waves that respect HA quorum, partition boundaries and migration blockers — with ready-to-apply MTV YAML per wave. Operators move VMs between waves and plans when they know something the inventory doesn't. See the **[Planning Quickstart](docs/PLANNING_QUICKSTART.md)**.
- **Validation (optional, needs SSH).** Build a behavioral baseline on the source VMs over days, then diff the migrated VMs against it and produce CISO-ready reports.

Either way, nothing leaves your network.

## Key Features

- **RVTools import** — server-side, 10,000 VMs per file, multi-vCenter routing, migratability assessment (RDM, shared disks, CBT, …) and environment detection at import
- **Resource mapping** — vSphere port groups / datastores → NADs, UDN/CUDN, StorageClasses, namespaces; **Import from cluster** from pasted `oc get` output; VLAN- and name-based suggestions; version-aware preflight for OCP 4.21/4.22
- **Migration planner** — deterministic wave packing (≤10 VMs per wave, ≤2 HA-family peers, one vCenter/cluster/namespace per wave, prod never mixed with non-prod), batches of up to 1,000 VMs per plan, parallel-safe wave groups; the LLM writes per-wave rationale and falls back to deterministic text without one
- **Manual plan edits** — move VMs between waves and plans, create waves, add and remove VMs; every edit is dry-run first, planner rules can be overridden with an audited confirmation, wave numbers never change
- **MTV YAML generation** — per-wave `NetworkMap`, `StorageMap` and `Plan`, validated offline against the Forklift CRD schemas; per-wave download or a zip bundle with apply instructions
- **Multi-day baseline collection** — captures services, network, storage, cron, and configuration state over a 3-7 day window
- **Intelligent post-migration validation** — diffs current state against baseline, LLM reasons over findings and produces remediation steps
- **Plain-English wave reports** — CISO-ready PDF reports with executive summaries and per-VM findings
- **Design review (network + storage)** — local-LLM gap analysis between VMware source environment and a proposed OpenShift Virtualization design. Network reviews cover CUDN / NAD / NetworkPolicy / Multus; storage reviews cover StorageClass / VolumeSnapshotClass / StorageMap (Forklift) including performance tier mismatches, replication loss, and access-mode gaps. See [docs/DESIGN_REVIEW.md](docs/DESIGN_REVIEW.md).
- **Air-gapped by design** — no outbound calls unless you configure a governed in-perimeter model endpoint; full audit trail

## Architecture

VirtValidate runs as a self-contained appliance inside your environment (Podman, or Helm on OpenShift). Planning needs only an RVTools export; validation adds read-only SSH to the VMs. It never connects to vCenter or to the cluster API.

```
┌──────────────┐         ┌────────────────────────────┐         ┌──────────────┐
│   VMware     │ ◄────── │   VirtValidate Appliance   │ ──────► │  OCP-Virt    │
│   vSphere    │         │                            │         │  (KubeVirt)  │
│              │   SSH   │  React · FastAPI           │   SSH   │              │
│  Source VMs  │ ──────► │  PostgreSQL · your LLM     │ ◄────── │  Target VMs  │
└──────────────┘         └────────────────────────────┘         └──────────────┘
   RVTools export ──────────────►  │
                                   ▼
                     Waves · MTV YAML · Wave Reports · Audit
```

SSH is used only for validation. The LLM backend is selectable at runtime in Settings: local **Ollama** (default), **KServe** / OpenShift AI, a governed **Model-as-a-Service** endpoint, or the **TrustyAI** guardrails orchestrator. Planning works with no LLM at all.

**[Full architecture diagram and feature catalog →](docs/product-map.html)**

## Quick Start

VirtValidate runs on any Linux host with Podman, or on OpenShift with the Helm chart ([deploy/README.md](deploy/README.md)).

### Requirements

- Podman 4.x+ and `podman-compose`
- 16 GB RAM minimum
- For a local model: ~35 GB disk and, ideally, an NVIDIA GPU (CPU works, slower). Not needed if you point at KServe / MaaS, or use planning without an LLM.

### Install

```bash
git clone https://github.com/cmalafis/virtvalidation
cd virtvalidation
cp .env.example .env

# Start the stack
podman-compose up -d

# Optional: pull a local LLM (~5GB, one-time). Skip for KServe/MaaS or planning-only.
podman exec $(podman ps -q --filter name=ollama) ollama pull llama3:8b

# Open the dashboard
open http://localhost:3000
```

That's it. Everything runs locally. No data leaves your machine.

### First run: planning only (RVTools → waves → YAML)

No SSH, no baselines. The Overview page tracks steps 1–4.

1. **Register each vCenter** — Configure → vCenter Sources. The hostname must match RVTools' *VI SDK Server* column; the name becomes the MTV source Provider name.
2. **Import RVTools** — Discover → Virtual Machines → Import VMs (*Export all to Excel* `.xlsx`; `vInfo` required)
3. **Register the cluster** — Configure → OCP Targets, then **Import from cluster** to fill its networks, storage classes and namespaces
4. **Map resources** — Configure → Resource Mappings → Pull from inventory, map every port group and datastore, pick a namespace strategy, Run preflight
5. **Generate plans** — Migrate → Migration Plans → Generate plan. The wizard previews the plans and every VM it will skip, with the reason.
6. **Adjust waves** — on the plan page: Move to wave, New wave from selection, Move to plan, Add VMs, Remove from plan
7. **Download YAML** — per wave, or **Download all YAML** for a zip with apply instructions
8. **Mark succeeded** once the plan's waves have migrated

**[Planning quickstart — every step in detail →](docs/PLANNING_QUICKSTART.md)**

### First run: planning + validation

Do the planning steps above, plus:

- **Generate the SSH key** — Settings → Compliance → Generate key, then add the public key to `~/.ssh/authorized_keys` on each VM (see [docs/SSH_SETUP.md](docs/SSH_SETUP.md))
- **Pick an inference backend** — Settings → Inference → Test connection
- **Capture baselines** — over a few days before migrating, so normal variation isn't mistaken for migration damage
- **Validate** each wave after MTV moves it, and read the report

**[Full user guide →](docs/USER_GUIDE.md)**

## Documentation

- [**Planning Quickstart**](docs/PLANNING_QUICKSTART.md) — RVTools → waves → manual edits → MTV YAML, no SSH required
- [**User Guide**](docs/USER_GUIDE.md) — end-to-end, from empty appliance to validated wave
- [**RVTools Guide**](docs/RVTOOLS_GUIDE.md) — sheets, columns, multi-vCenter imports, limits
- [**Resource Mapping**](docs/RESOURCE_MAPPING.md) — catalogs, Import from cluster, suggestions, preflight checks
- [**Planning Guide**](docs/PLANNING_GUIDE.md) — wave rules, plan series, the plan-edit API
- [**Environment Labels**](docs/ENVIRONMENT_LABELS.md) — how prod / dev / DR are detected and why plans split on them
- [**Migratability Assessment**](docs/MIGRATABILITY_ASSESSMENT.md) — why a VM is blocked from planning
- [**Inventory Guide**](docs/INVENTORY_GUIDE.md) — the VM table, filters, CSV import
- [**Product Map**](docs/product-map.html) — what the product does (feature catalog, roadmap)
- [**Architecture Diagram**](docs/architecture-diagram.html) — how it's built (interactive module + component map, regenerated from source)
- [**Architecture Reference**](docs/ARCHITECTURE.md) — markdown view of the same data
- [**Installation Guide**](docs/INSTALLATION.md) — detailed setup including production Quadlet deployment
- [**OpenShift / Helm deployment**](deploy/README.md) — chart values, KServe and MaaS backends, hardened images
- [**SSH Setup**](docs/SSH_SETUP.md) — distributing keys, sudoers configuration, security model
- [**Windows Setup**](docs/WINDOWS_SETUP.md) — OpenSSH + PowerShell configuration for Windows Server VMs
- [**OS Compatibility Matrix**](docs/COMPATIBILITY.md) — RHEL 7/8/9/10, Rocky, Alma, CentOS, Ubuntu/Debian, Windows Server 2019/2022/2025 status
- [**Design Review**](docs/DESIGN_REVIEW.md) — local-LLM gap analysis for both network (CUDN/NAD/NetworkPolicy) and storage (StorageClass/VolumeSnapshotClass) designs
- [**Configuration Reference**](docs/CONFIGURATION.md) — all environment variables explained
- [**API Reference**](docs/API.md) — REST endpoint documentation
- [**Security Model**](SECURITY.md) — threat model and reporting vulnerabilities

## Roadmap

VirtValidate is in active development. Current focus: production hardening for v0.2.0.

| Release | Status | Theme |
|---------|--------|-------|
| **v0.1.0-alpha** | ✅ Shipped | Foundation — full end-to-end MVP |
| **v0.2.0** | 🔄 In Progress | Polish & hardening, encrypted keys, app probes |
| **v0.3.0** | 📋 Planned | Ansible playbook generation, vCenter discovery |
| **v1.0.0** | 🎯 Target | Multi-tenancy, RBAC, direct vLLM backend, GA |
| **v2.0.0** | 🔮 Future | Federal classified — Vault, HSM/TPM, CAC/PIV, FIPS |

See the [GitHub Projects board](https://github.com/cmalafis/virtvalidation/projects) for detailed work tracking and the [product map](docs/product-map.html) for the complete feature catalog.

## Contributing

Contributions are welcome. VirtValidate follows standard open source practices:

- Read [CONTRIBUTING.md](CONTRIBUTING.md) for development setup and PR process
- Check the [issues](https://github.com/cmalafis/virtvalidation/issues) labeled `good first issue`
- Join the discussion in [GitHub Discussions](https://github.com/cmalafis/virtvalidation/discussions)

All contributors must agree to the [Code of Conduct](CODE_OF_CONDUCT.md) and sign off commits per the DCO.

## Security

VirtValidate is built for federal, defense, and regulated environments. Two image variants ship:

- **Standard (UBI 9)** — Red Hat Universal Base Image 9, digest-pinned, FIPS-compatible. Default.
- **Hardened (Red Hat Hardened Images)** — minimal, signed, SBOM-embedded, near-zero-CVE. Opt-in via `helm install --set image.variant=hardened`.

Automated CI gates and security scanning (SAST/SCA, image scanning, dependency updates) are planned for a future milestone and are not currently active — they were deferred as premature during early development. For now, development relies on local `ruff`/`pytest` discipline and a blocking `gitleaks` pre-commit hook to keep credentials out of history.

Found a security issue? Please **do not open a public issue.** See [SECURITY.md](SECURITY.md) for responsible disclosure.

## License

Apache License 2.0 — see [LICENSE](LICENSE).

VirtValidate is built on the shoulders of giants. Thanks to the maintainers of [Ollama](https://ollama.ai), [Llama](https://llama.meta.com), [KServe](https://kserve.github.io/website/), [FastAPI](https://fastapi.tiangolo.com), [Forklift/MTV](https://github.com/kubev2v/forklift), [Podman](https://podman.io), and [PostgreSQL](https://www.postgresql.org).

---

<div align="center">

**Built for federal, defense, and regulated environments where data cannot leave the network.**

</div>
