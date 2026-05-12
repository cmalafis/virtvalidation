# Installation

VirtValidate runs as a four-container appliance: React frontend, FastAPI
backend, PostgreSQL, and Ollama. Everything is deployed with **Podman** —
not Docker. The defaults assume RHEL 9 / Fedora 40+, but any Linux host
with rootless Podman 4.4+ will work.

> Air-gapped by design — no container ever calls outbound APIs at runtime.
> The only network egress happens during the initial image pulls and the
> Llama 3 model pull described below.

---

## 1. Prerequisites

| Component | Minimum | Notes |
|-----------|---------|-------|
| OS | RHEL 9, Fedora 40+, or Ubuntu 22.04+ | macOS works for development via `podman machine` |
| Podman | 4.4+ | `podman --version` |
| podman-compose | 1.0.6+ | `pip install podman-compose` or distro package |
| Disk | 30 GB free | Llama 3 8B is ~4.7 GB; PostgreSQL volume grows with snapshots |
| RAM | 16 GB | Llama 3 8B needs ~6 GB; the rest is for the OS + frontend |
| GPU (optional) | NVIDIA with CUDA 12+ | CPU inference works, just slower |

Verify rootless Podman is healthy:

```bash
podman info | grep rootless
podman run --rm docker.io/hello-world
```

If those work, you're set.

---

## 2. Clone and configure

```bash
git clone https://github.com/your-org/virtvalidate.git
cd virtvalidate
cp .env.example .env
$EDITOR .env
```

At minimum, change `POSTGRES_PASSWORD` and `SECRET_KEY` to non-default values.
See [CONFIGURATION.md](CONFIGURATION.md) for every variable.

---

## 3. Generate the SSH key VirtValidate uses to reach VMs

VirtValidate SSHes into each enrolled VM using a single keypair
stored on the appliance. **The private key never leaves the host.**

### Recommended: Generate from the Settings page

Open the dashboard at `http://localhost:3000`, click into **Settings**,
and use the **Generate SSH Key** button. The Settings page handles
algorithm selection, FIPS gating, and surfaces enrollment snippets
for Ansible / Puppet / Terraform automatically. No pod shell access
required — works for both podman-compose and OpenShift deployments.

See [`docs/SSH_KEY_GUIDE.md`](./SSH_KEY_GUIDE.md) for the full
lifecycle including rotation and backup.

### CLI fallback (dev only)

If you'd rather pre-stage the key before bringing the appliance up:

```bash
mkdir -p ./backend/app/keys
ssh-keygen -t ed25519 -N "" -f ./backend/app/keys/id_ed25519 -C "virtvalidate-appliance"
chmod 600 ./backend/app/keys/id_ed25519
chmod 644 ./backend/app/keys/id_ed25519.pub
```

Distribute the **public** half to every VM you intend to enroll.

---

## 4. Pull the Llama 3 model

This is the one moment VirtValidate touches the public internet —
downloading the model weights. After this you can take the appliance
fully offline.

```bash
podman volume create ollamadata
podman run --rm \
    -v ollamadata:/root/.ollama:Z \
    docker.io/ollama/ollama:latest \
    serve &

# In another shell, pull the model:
podman exec -it $(podman ps -q --filter ancestor=docker.io/ollama/ollama:latest) \
    ollama pull llama3:8b
```

Or simpler — let the compose file start Ollama and pull from inside:

```bash
podman-compose up -d ollama
podman-compose exec ollama ollama pull llama3:8b
```

Switching models later is a one-click change on the **Settings** page —
see [CONFIGURATION.md](CONFIGURATION.md#ollama_model).

---

## 5. (Optional) GPU acceleration

NVIDIA GPUs deliver ~10× the throughput on the validation/planning prompts.

### a. Install the NVIDIA Container Toolkit

```bash
# RHEL/Fedora
sudo dnf install -y nvidia-container-toolkit
# Ubuntu
sudo apt-get install -y nvidia-container-toolkit
```

### b. Generate the CDI spec for Podman

```bash
sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml
podman info | grep -A2 "CDI"   # confirm Podman sees the spec
```

### c. Enable the GPU on the Ollama container

In `podman-compose.yml`, uncomment the device block under the `ollama`
service:

```yaml
services:
  ollama:
    # ...
    devices:
      - nvidia.com/gpu=all
```

Restart Ollama only:

```bash
podman-compose up -d --force-recreate ollama
podman exec ollama nvidia-smi   # confirm the GPU is visible inside
```

---

## 6. Start the appliance

```bash
podman-compose up -d
podman-compose ps
```

You should see four containers running: `frontend`, `backend`, `postgres`, `ollama`.

First-run health check:

```bash
curl -s http://localhost:8000/health
# {"status":"ok","version":"0.1.0"}

curl -s http://localhost:8000/api/health/ollama | jq
# {"status":"online","host":"http://ollama:11434","version":"...","latency_ms":12}

curl -s http://localhost:8000/api/health/postgres | jq
# {"status":"online","latency_ms":1}
```

Open the dashboard at <http://localhost:3000>. You should land on the
empty-state CTA prompting you to enroll your first VM.

---

## 7. Production deployment with Quadlet

For systemd-managed deployment on RHEL/Fedora, use the Quadlet units
under `infra/quadlet/`:

```bash
mkdir -p ~/.config/containers/systemd
mkdir -p ~/.config/virtvalidate
cp .env ~/.config/virtvalidate/env

cp infra/quadlet/*.container ~/.config/containers/systemd/
cp infra/quadlet/*.network   ~/.config/containers/systemd/

systemctl --user daemon-reload
systemctl --user enable --now virtvalidate-backend.service
systemctl --user status virtvalidate-backend.service
```

The Quadlet units mount the SSH keys from
`~/.local/share/virtvalidate/keys`. Move yours there before starting.

---

## 8. Backups

Two volumes need to be backed up: the PostgreSQL database (snapshots,
plans, audit log) and the SSH keys.

```bash
# Postgres dump
podman-compose exec postgres pg_dump -U virtvalidate virtvalidate \
    | gzip > backup-$(date +%F).sql.gz

# SSH keys (treat as secret material)
tar czf keys-$(date +%F).tar.gz backend/app/keys/
```

Keep dumps and key tarballs encrypted at rest and rotate the SSH keypair
annually.

---

## 9. Troubleshooting

| Symptom | Likely cause / fix |
|---------|-------------------|
| `permission denied: /app/keys/id_ed25519` | SELinux mislabeled volume — make sure the volume mount uses the `:Z` flag (it does in the shipped compose file). On RHEL 9, run `restorecon -RFvv backend/app/keys`. |
| Backend keeps restarting | `podman-compose logs backend` — usually `connection refused` to Postgres on first boot. Wait 5 s and retry, or set `depends_on.condition: service_healthy`. |
| `/api/health/ollama` returns offline | The model isn't loaded yet. Run the model pull from step 4. |
| Frontend shows `Failed to load VMs from backend` | Vite proxy can't reach the backend. Confirm `VITE_API_URL` points to a host the browser can reach (default `http://localhost:8000`). |
| Frontend nginx upstream errors / 502 from /api/ | The frontend image now templates `nginx.conf` via `envsubst` at start. Set `BACKEND_HOST` + `BACKEND_PORT` (defaults: `backend` / `8000` in compose; the Helm chart wires them automatically). |
| Backend pod logs `password authentication failed for user "$(POSTGRES_USER)"` | Env var ordering — see [DEPLOYMENT_TROUBLESHOOTING.md §1](./DEPLOYMENT_TROUBLESHOOTING.md). The chart fixed this in 0.1.1; `helm upgrade` if you installed an earlier version. |
| Slow LLM responses on CPU | Expected. Switch to GPU (step 5) or pick a smaller model in **Settings → LLM Model**. |
| Cross-arch image build crashes on Apple Silicon | `./scripts/build-images.sh` auto-detects + falls back to a native host build + runtime-only image. Override with `PLATFORM=linux/arm64` for native dev builds. |

For deeper issues, the full audit trail is at
<http://localhost:3000/audit-log> (use the *audit log* tab on the
dashboard). Every API mutation is recorded.

For OpenShift-specific deployment issues — env var ordering, Service
name resolution in nginx, emptyDir mount overlay surprises,
cross-architecture builds — see [DEPLOYMENT_TROUBLESHOOTING.md](./DEPLOYMENT_TROUBLESHOOTING.md).

---

## Templated nginx config

As of chart 0.1.1, the frontend image ships `nginx.conf.template`
(not `nginx.conf`). At container start, `entrypoint.sh` runs
`envsubst` against the template, substituting two placeholders:

| Env var        | Default (entrypoint) | Compose value | Helm chart value |
|----------------|----------------------|---------------|------------------|
| `BACKEND_HOST` | `backend`            | `backend`     | `<release>-backend` |
| `BACKEND_PORT` | `8000`               | `8000`        | `.Values.backend.service.port` |

This lets the same image work for `podman-compose up` and a
namespaced `helm install <release>` without rebuilding. Override
in either environment by setting the env vars on the frontend
container.
