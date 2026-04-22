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
vi .env

# 2. Pull Llama 3 (first run only)
podman run --rm docker.io/ollama/ollama pull llama3:8b

# 3. Start everything
podman-compose up -d

# 4. Open the dashboard
open http://localhost:3000
```

## Requirements

- Podman 4.x+
- podman-compose
- NVIDIA GPU recommended (CPU inference works, slower)
- RHEL 9 / Fedora 40+ recommended

## Architecture

```
┌─────────────────────────────────────┐
│  React Dashboard  :3000             │
├─────────────────────────────────────┤
│  FastAPI Backend  :8000             │
├──────────────┬──────────────────────┤
│  PostgreSQL   │  Ollama + Llama 3   │
│  :5432        │  :11434             │
└──────────────┴──────────────────────┘
All containers run rootless via Podman.
No data leaves this machine.
```

## License

Proprietary — © 2025 VirtValidate
