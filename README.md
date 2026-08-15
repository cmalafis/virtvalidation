<div align="center">

<img src="docs/assets/logo.svg" alt="VirtValidate" width="120" />

# VirtValidate

**AI-powered VM migration validation for VMware → OpenShift Virtualization**

Capture pre-migration baselines, generate intelligent migration waves with MTV-ready YAML, and validate post-migration state — all running air-gapped with local AI inference.

[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![Release](https://img.shields.io/github/v/release/cmalafis/virtvalidation?include_prereleases)](https://github.com/cmalafis/virtvalidation/releases)
[![Python](https://img.shields.io/badge/python-3.12-3776AB?logo=python&logoColor=white)](https://www.python.org)
[![Podman](https://img.shields.io/badge/podman-rootless-892CA0?logo=podman&logoColor=white)](https://podman.io)
[![UBI 9](https://img.shields.io/badge/base-Red%20Hat%20UBI%209-EE0000?logo=redhat&logoColor=white)](docs/CONTAINER_IMAGES.md)

[Quick Start](#quick-start) · [Product Map](docs/product-map.html) · [Architecture](docs/architecture-diagram.html) · [Roadmap](https://github.com/cmalafis/virtvalidation/projects) · [Contributing](CONTRIBUTING.md)

</div>

---

## Why VirtValidate

VMware-to-OpenShift Virtualization migrations involve hundreds or thousands of VMs and almost no tooling for the most critical question: **did each VM actually migrate correctly?**

Existing tools handle the mechanics of migration — moving disks, recreating VMs, mapping networks. None of them tell you whether the migrated VM is actually healthy, configured the same as before, and ready for production traffic.

VirtValidate fills that gap. It SSHes into source VMs to build a behavioral baseline over days, intelligently groups VMs into migration waves with ready-to-apply MTV YAML, then validates post-migration state and produces CISO-ready reports — all without sending a single byte to the cloud.

## Key Features

- **Multi-day baseline collection** — captures services, network, storage, cron, and configuration state over a 3-7 day window
- **AI-powered migration planner** — local Llama 3 groups VMs into waves based on application dependencies, network mappings, and storage constraints
- **MTV YAML generation** — produces ready-to-apply Forklift `Plan`, `NetworkMap`, and `StorageMap` resources per wave
- **Intelligent post-migration validation** — diffs current state against baseline, LLM reasons over findings and produces remediation steps
- **Plain-English wave reports** — CISO-ready PDF reports with executive summaries and per-VM findings
- **Design review (network + storage)** — local-LLM gap analysis between VMware source environment and a proposed OpenShift Virtualization design. Network reviews cover CUDN / NAD / NetworkPolicy / Multus; storage reviews cover StorageClass / VolumeSnapshotClass / StorageMap (Forklift) including performance tier mismatches, replication loss, and access-mode gaps. See [docs/DESIGN_REVIEW.md](docs/DESIGN_REVIEW.md).
- **Air-gapped by design** — zero external API calls, all inference local, full audit trail

## Architecture

VirtValidate runs as a self-contained Podman appliance inside your environment. SSH into source VMs, validate target VMs, all AI inference local.

```
┌──────────────┐         ┌────────────────────────────┐         ┌──────────────┐
│   VMware     │ ◄────── │   VirtValidate Appliance   │ ──────► │  OCP-Virt    │
│   vSphere    │         │                            │         │  (KubeVirt)  │
│              │   SSH   │  React · FastAPI · Ollama  │   SSH   │              │
│  Source VMs  │ ──────► │  PostgreSQL · Llama 3 8B   │ ◄────── │  Target VMs  │
└──────────────┘         └────────────────────────────┘         └──────────────┘
                                       │
                                       ▼
                         Wave Reports · MTV YAML · Audit
```

**[Full architecture diagram and feature catalog →](docs/product-map.html)**

## Quick Start

VirtValidate runs on any Linux host with Podman. NVIDIA GPU recommended for inference performance, but CPU works.

### Requirements

- Podman 4.x+ and `podman-compose`
- 16 GB RAM minimum
- 35 GB disk for AI model + database
- NVIDIA GPU recommended (CPU inference works, slower)

### Install

```bash
git clone https://github.com/cmalafis/virtvalidation
cd virtvalidation
cp .env.example .env

# Start the stack
podman-compose up -d

# Pull the local LLM (~5GB, one-time)
podman exec $(podman ps -q --filter name=ollama) ollama pull llama3:8b

# Open the dashboard
open http://localhost:3000
```

That's it. Everything runs locally. No data leaves your machine.

### First Run

The Overview page tracks these steps and links to whichever one is next.

1. **Generate the SSH key** — Settings → Compliance → Generate key, then add the public key to `~/.ssh/authorized_keys` on each VM (see [docs/SSH_SETUP.md](docs/SSH_SETUP.md))
2. **Pick an inference backend** — Settings → Inference → Test connection
3. **Register a vCenter source** — Configure → vCenter Sources
4. **Register an OpenShift target** — Configure → OCP Targets, then fill in its networks, storage classes and namespaces
5. **Import inventory** — Discover → Virtual Machines → Import VMs (RVTools XLSX, CSV, or manual)
6. **Map resources** — Configure → Resource Mappings, then run preflight
7. **Capture baselines** — over a few days, so normal variation isn't mistaken for migration damage
8. **Generate a plan** — Migrate → Migration Plans → Generate plan → download the MTV YAML
9. **Migrate and validate** — apply the YAML with Forklift/MTV, then validate the wave and read the report

**[Full user guide →](docs/USER_GUIDE.md)**

## Documentation

- [**User Guide**](docs/USER_GUIDE.md) — end-to-end, from empty appliance to validated wave
- [**Product Map**](docs/product-map.html) — what the product does (feature catalog, roadmap)
- [**Architecture Diagram**](docs/architecture-diagram.html) — how it's built (interactive module + component map, regenerated from source)
- [**Architecture Reference**](docs/ARCHITECTURE.md) — markdown view of the same data
- [**Installation Guide**](docs/INSTALLATION.md) — detailed setup including production Quadlet deployment
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
| **v0.3.0** | 📋 Planned | Ansible playbook generation, vCenter discovery, Helm chart |
| **v1.0.0** | 🎯 Target | Multi-tenancy, RBAC, vLLM, GA |
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

VirtValidate is built on the shoulders of giants. Thanks to the maintainers of [Ollama](https://ollama.ai), [Llama](https://llama.meta.com), [FastAPI](https://fastapi.tiangolo.com), [Forklift/MTV](https://github.com/kubev2v/forklift), [Podman](https://podman.io), and [PostgreSQL](https://www.postgresql.org).

---

<div align="center">

**Built for federal, defense, and regulated environments where data cannot leave the network.**

</div>
