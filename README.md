<div align="center">

<img src="docs/assets/logo.svg" alt="VirtValidate" width="120" />

# VirtValidate

**AI-powered VM migration validation for VMware → OpenShift Virtualization**

Capture pre-migration baselines, generate intelligent migration waves with MTV-ready YAML, and validate post-migration state — all running air-gapped with local AI inference.

[![CI](https://github.com/cmalafis/virtvalidation/actions/workflows/ci.yml/badge.svg)](https://github.com/cmalafis/virtvalidation/actions/workflows/ci.yml)
[![CodeQL](https://github.com/cmalafis/virtvalidation/actions/workflows/codeql.yml/badge.svg)](https://github.com/cmalafis/virtvalidation/actions/workflows/codeql.yml)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![Release](https://img.shields.io/github/v/release/cmalafis/virtvalidation?include_prereleases)](https://github.com/cmalafis/virtvalidation/releases)
[![Python](https://img.shields.io/badge/python-3.12-3776AB?logo=python&logoColor=white)](https://www.python.org)
[![Podman](https://img.shields.io/badge/podman-rootless-892CA0?logo=podman&logoColor=white)](https://podman.io)

[Quick Start](#quick-start) · [Product Map](docs/product-map.html) · [Roadmap](https://github.com/cmalafis/virtvalidation/projects) · [Contributing](CONTRIBUTING.md)

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

1. **Generate SSH key** — Settings → SSH Public Key → key auto-generates on first load
2. **Distribute key to VMs** — copy the public key into `~/.ssh/authorized_keys` on each VM you want to monitor (see [docs/SSH_SETUP.md](docs/SSH_SETUP.md))
3. **Enroll your VMs** — Add VMs button → Manual, CSV, or RVTools XLSX upload
4. **Watch the baseline build** — automatic SSH collection runs twice daily by default
5. **Plan your migration** — Migration Plan tab → Generate Plan → Download MTV YAML
6. **Migrate and validate** — apply the YAML with MTV, click Mark Wave Complete, get your report

## Documentation

- [**Product Map**](docs/product-map.html) — full architecture, feature catalog, roadmap
- [**Installation Guide**](docs/INSTALLATION.md) — detailed setup including production Quadlet deployment
- [**SSH Setup**](docs/SSH_SETUP.md) — distributing keys, sudoers configuration, security model
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
| **v1.0.0** | 🎯 Target | Multi-tenancy, RBAC, vLLM, Windows VMs, GA |
| **v2.0.0** | 🔮 Future | Federal classified — Vault, HSM/TPM, CAC/PIV, FIPS |

See the [GitHub Projects board](https://github.com/cmalafis/virtvalidation/projects) for detailed work tracking and the [product map](docs/product-map.html) for the complete feature catalog.

## Contributing

Contributions are welcome. VirtValidate follows standard open source practices:

- Read [CONTRIBUTING.md](CONTRIBUTING.md) for development setup and PR process
- Check the [issues](https://github.com/cmalafis/virtvalidation/issues) labeled `good first issue`
- Join the discussion in [GitHub Discussions](https://github.com/cmalafis/virtvalidation/discussions)

All contributors must agree to the [Code of Conduct](CODE_OF_CONDUCT.md) and sign off commits per the DCO.

## Security

Found a security issue? Please **do not open a public issue.** See [SECURITY.md](SECURITY.md) for responsible disclosure.

## License

Apache License 2.0 — see [LICENSE](LICENSE).

VirtValidate is built on the shoulders of giants. Thanks to the maintainers of [Ollama](https://ollama.ai), [Llama](https://llama.meta.com), [FastAPI](https://fastapi.tiangolo.com), [Forklift/MTV](https://github.com/kubev2v/forklift), [Podman](https://podman.io), and [PostgreSQL](https://www.postgresql.org).

---

<div align="center">

**Built for federal, defense, and regulated environments where data cannot leave the network.**

</div>
