# VirtValidate

> AI-powered VM migration validation for VMware → OpenShift Virtualization

VirtValidate is a self-hosted appliance that validates migrated VMs are
running correctly after migration from VMware to OpenShift Virtualization.
It uses local LLM inference (Ollama + Llama 3) to reason over pre/post
migration state — no data ever leaves your network.

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

## License

Proprietary — © 2026 VirtValidate
