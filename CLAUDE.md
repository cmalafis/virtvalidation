# VirtValidate — AI VM Migration Validation Platform

## What this is
Local appliance that validates VMs migrated from VMware to OpenShift
Virtualization using SSH + local LLM reasoning. Air-gapped by design.

## Stack
- Frontend: React 18 + Tailwind + Vite
- Backend: Python 3.12 + FastAPI
- LLM: Ollama + Llama 3 8B (local, never calls external APIs)
- DB: PostgreSQL 16
- Containers: Podman (rootless) + podman-compose for dev, Quadlet for prod
- SSH: Paramiko + Ed25519 keys

## Core modules
1. Pre-flight capture — SSH into VMware VMs, collect baseline state
2. Validation engine — SSH into OCP-Virt VMs, diff vs baseline, LLM reasons
3. Migration planner — LLM groups VMs into dependency-ordered waves

## Hard rules
- NEVER call external APIs or LLM services — air-gapped by design
- All LLM calls go to Ollama at $OLLAMA_HOST (default: http://ollama:11434)
- SSH uses Ed25519 keys only — stored in /app/keys/, never baked into images
- Use Podman — NOT Docker. Containerfiles NOT Dockerfiles.
- Volume mounts use :Z SELinux label for RHEL/Fedora compatibility
- Images always reference docker.io/ or registry.access.redhat.com/ explicitly

## Architecture documentation
- `docs/ARCHITECTURE.md` and `docs/architecture-diagram.html` are
  **generated** from source by `scripts/generate_architecture_docs.py`.
- When you add a new function, class, API endpoint, SQLAlchemy model,
  Pydantic schema, or React component, regenerate before committing:
  `python3 scripts/generate_architecture_docs.py`
- CI (`.github/workflows/architecture-docs.yml`) regenerates on every
  PR and fails the build if the committed docs differ — drift is caught
  at review time, not in production.
- `docs/product-map.html` is hand-maintained — describes *what* the
  product does. The architecture diagram describes *how* it's built.

## Repo structure
virtvalidate/
├── frontend/          # React app
├── backend/           # FastAPI
│   └── app/
│       ├── api/       # Route handlers
│       ├── core/      # SSH engine, LLM client
│       ├── models/    # SQLAlchemy models
│       └── main.py
├── infra/
│   └── quadlet/       # Systemd Quadlet units for prod deployment
├── podman-compose.yml # Dev: one command to run everything
├── CLAUDE.md          # This file — read at start of every session
└── .env.example       # Config template

## Current status
- [x] Frontend dashboard (React) — VirtValidate.jsx
- [ ] Backend API skeleton
- [ ] SSH collection engine
- [ ] LLM validation engine
- [ ] Migration planner
- [ ] PostgreSQL models

## DEFENSIVE CODING REQUIREMENTS

When writing React components that consume API data, ALWAYS:

1. Assume any nested field might be undefined or null
2. Use optional chaining (?.) and nullish coalescing (??) for all 
   nested property access
3. Provide empty array defaults: const items = data?.items ?? []
4. Render explicit empty states for missing data, never let the 
   component crash
5. Wrap pages in error boundaries
6. Test components with: no data, partial data, error responses, 
   loading states

Common patterns that crash:
- array.length when array is undefined → use (array || []).length
- object.field when object is undefined → use object?.field
- array.map(...) when array is undefined → use (array || []).map(...)
- nested.path.access → use nested?.path?.access

When writing API endpoints, ALWAYS return consistent response 
shapes. Don't return 404 for "no data yet" — return 200 with 
empty arrays/null fields. The frontend should never have to 
distinguish between "endpoint doesn't exist" and "endpoint exists 
but no data."
