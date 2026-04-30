# `scripts/`

Operator-facing utilities. Not part of the runtime; run them by hand or
from `make` targets.

| Script | Purpose |
|--------|---------|
| `generate_api_docs.py` | Re-generate `docs/API.md` from the live FastAPI OpenAPI spec |
| `test_ssh.py`          | Manual smoke test for the SSH collection engine |
| `test_validation.py`   | End-to-end smoke test: baseline → current → LLM verdict |

## `test_ssh.py`

CLI wrapper around `app.core.ssh.SSHCollector`. SSHes into a real VM with
the appliance's Ed25519 key, runs the production collection routine, and
validates the resulting JSON shape. Useful for verifying SSH plumbing on
a freshly enrolled VM before letting the scheduled baseline pass touch
it.

> Not a pytest test. The pytest suite uses an in-memory mock SSH layer.
> This script exists for the times you need to confirm "yes, the key
> works against *this specific VM*."

### Running it

#### From the repo root (developer workstation)

The script auto-detects the `backend/` layout, so you only need a Python
environment with the backend dependencies installed.

```bash
# One-time: Python deps (paramiko, etc.)
cd backend
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cd ..

# Smoke test the connection
export TEST_VM_HOST=10.20.30.10
export TEST_VM_USER=virtvalidate            # optional — this is the default
export SSH_KEY_PATH=backend/app/keys/id_ed25519
python3 scripts/test_ssh.py
```

#### Inside the running backend container

`scripts/` is not baked into the image, so copy the script in (or volume-mount it)
before running:

```bash
podman cp scripts/test_ssh.py virtvalidation-backend:/tmp/test_ssh.py
podman exec -it -e TEST_VM_HOST=10.20.30.10 \
    virtvalidation-backend \
    python3 /tmp/test_ssh.py
```

The container's `WORKDIR` is `/app`, so the script will find
`app.core.ssh` automatically. The Ed25519 key it uses is the same one
the appliance uses at runtime (`/app/keys/id_ed25519` by default).

### Arguments and environment variables

Every flag has an environment-variable fallback so the same invocation
works in CI, in the container, and on a workstation.

| Flag | Env var | Default | Purpose |
|------|---------|---------|---------|
| `--host` | `TEST_VM_HOST` | — (required) | VM IP or hostname |
| `--user` | `TEST_VM_USER` | `virtvalidate` | SSH username |
| `--port` | `TEST_VM_PORT` | `22` | SSH port |
| `--key`  | `SSH_KEY_PATH` | `/app/keys/id_ed25519` | Ed25519 private key |
| `--output` | — | stdout | Write JSON to this path instead |
| `--quiet` | — | off | Suppress progress + validation summary on stderr |

Run `python3 scripts/test_ssh.py --help` for the full reference.

### Output

By default, pretty-printed JSON goes to **stdout** and progress + the
validation summary go to **stderr**, so you can pipe one without
swallowing the other:

```bash
python3 scripts/test_ssh.py --host 10.20.30.10 > /tmp/baseline.json
# or
python3 scripts/test_ssh.py --host 10.20.30.10 --output /tmp/baseline.json
```

The validation summary reports two things:

1. **Required top-level keys** — every key here must be present for the
   script to exit `0`. Mirrors what `SSHCollector.collect()` actually
   returns today: `meta`, `services`, `network`, `ports`, `mounts`,
   `cron`.
2. **Aspirational fields (informational)** — keys the platform plans to
   eventually emit (`storage`, `processes`, `users`, `os_info`,
   `performance`, `config_hashes`). These are reported as a TODO list
   but do not fail the smoke test. As the collector grows new sections
   they should be promoted from this list to the required gate inside
   `scripts/test_ssh.py`.

### Exit codes

| Code | Meaning |
|------|---------|
| `0`  | Connection succeeded and all required top-level keys are present |
| `1`  | SSH failure, unexpected exception, or a required key is missing |
| `2`  | Bad CLI arguments (e.g. no `--host` given) or `app.core.ssh` not importable |
| `130`| Interrupted (Ctrl-C) |

### Common failure modes

The script tries to suggest the right fix for each error category. The
remediation block goes to **stderr** so it doesn't pollute the JSON on
stdout.

#### "SSH key not found at …"

The path passed via `--key` (or `SSH_KEY_PATH`) doesn't exist. Either
generate one:

```bash
ssh-keygen -t ed25519 -N '' -f backend/app/keys/id_ed25519 \
    -C "virtvalidate@appliance"
```

…or point the script at an existing key.

#### "Authentication failed" / "Permission denied (publickey)"

The public side of the key isn't in the VM's `authorized_keys`, or the
user doesn't have permission to run the commands the collector uses.

Add the key:

```bash
ssh-copy-id -i backend/app/keys/id_ed25519.pub \
    -p 22 virtvalidate@10.20.30.10
```

The collector currently runs `systemctl list-units`, `ip addr/route`,
`cat /etc/resolv.conf`, `ss -tulnH`, `findmnt`, `crontab -l -u <user>`,
and a few `cat`s in `/etc/cron.*`. If the user isn't allowed sudo-less
access to those, add a sudoers rule or run as root. Example sudoers
fragment:

```
virtvalidate ALL=(ALL) NOPASSWD: /bin/systemctl, /usr/bin/ss, /bin/findmnt
```

See [`docs/SSH_SETUP.md`](../docs/SSH_SETUP.md) for the canonical key
distribution and sudoers playbook.

#### "Connection timed out"

Network/firewall path is blocked. Verify reachability from wherever
you're running the script:

```bash
nc -vz 10.20.30.10 22
```

If the VM is on a private subnet that only the appliance can reach, run
the script *inside* the backend container.

#### "Unknown host key" / "RejectPolicy"

The collector uses Paramiko's strict `RejectPolicy()` — unknown host
keys are refused. Capture the host key into `~/.ssh/known_hosts` first:

```bash
ssh-keyscan -p 22 10.20.30.10 >> ~/.ssh/known_hosts
```

Inside the container the same file lives at
`/root/.ssh/known_hosts` (or wherever `$HOME` resolves for the running
user).

## `test_validation.py`

End-to-end smoke test for the full validation loop. Reuses the
production `app.core.ssh.SSHCollector` and `app.core.llm.LLMClient` —
no logic is reimplemented in the script. The script:

1. SSHes into the **source** VM and captures a "pre-migration" baseline.
2. Optionally pauses so you can hand-edit the VM to simulate drift.
3. SSHes into the **target** VM and captures a "post-migration" state.
4. Hands both states to Ollama via `LLMClient.validate()` and prints
   the verdict (HEALTHY / DEGRADED / FAILED + per-finding remediation).

All three intermediate artifacts are written to disk (`baseline.json`,
`current.json`, `verdict.json`) so you can re-inspect or replay them.

### Single-VM workflow (most common)

When you only have one VM to test against, point both `source` and
`target` at it and let the pause prompt simulate "migration drift".
Run from your workstation with the appliance backend deps installed:

```bash
# Capture baseline → modify the VM by hand → capture current → verdict
export TEST_VM_HOST=10.20.30.10
export SSH_KEY_PATH=backend/app/keys/id_ed25519
export OLLAMA_HOST=http://localhost:11434     # if Ollama is on localhost
python3 scripts/test_validation.py --role database
```

When the script pauses, in another terminal SSH into the VM and break
something:

```bash
ssh virtvalidate@10.20.30.10
sudo systemctl stop postgresql       # stop a service
sudo crontab -u root -r              # remove root's cron
exit
```

Then press Enter back in the validation script. Expected verdict:
`DEGRADED` or `FAILED` with findings naming the stopped service and
the missing crontab.

### Two-VM workflow (real migration smoke test)

Use this against an actual VMware → OCP-Virt migration to prove the
appliance reaches both endpoints:

```bash
python3 scripts/test_validation.py \
    --source-host db-prod-01.vmware.local \
    --source-user virtvalidate \
    --target-host db-prod-01.ocp.local \
    --target-user virtvalidate \
    --role database \
    --no-pause-between-captures \
    --output-dir ./out/db-prod-01
```

`--no-pause-between-captures` is required for unattended/CI runs.

### Inside the backend container

`scripts/` is not baked into the image. Copy the script in alongside
the SSH helper, then exec:

```bash
podman cp scripts/test_validation.py virtvalidation-backend:/tmp/
podman exec -it \
    -e TEST_VM_HOST=10.20.30.10 \
    -e OLLAMA_HOST=http://ollama:11434 \
    virtvalidation-backend \
    python3 /tmp/test_validation.py --role database --no-pause-between-captures
```

Inside the container `OLLAMA_HOST` should point at the `ollama` service
on the podman-compose network (default `http://ollama:11434`).

### Arguments and environment variables

| Flag | Env var | Default | Purpose |
|------|---------|---------|---------|
| `--source-host` | `TEST_VM_HOST` | — (required) | Source VM (baseline) |
| `--source-user` | `TEST_VM_USER` | `virtvalidate` | Source SSH user |
| `--source-port` | `TEST_VM_PORT` | `22` | Source SSH port |
| `--target-host` | `TEST_VM_HOST` | source-host | Target VM (current state) |
| `--target-user` | `TEST_VM_USER` | `virtvalidate` | Target SSH user |
| `--target-port` | `TEST_VM_PORT` | `22` | Target SSH port |
| `--key`         | `SSH_KEY_PATH` | `/app/keys/id_ed25519` | Ed25519 private key |
| `--role`        | — | `unspecified` | Role hint passed to the LLM |
| `--llm-timeout` | — | `120` (s) | Bump on CPU-only hosts |
| `--pause-between-captures` / `--no-…` | — | on | Pause prompt before second capture |
| `--output-dir`  | — | `./test_output` | Where the three JSON files land |
| `--quiet`       | — | off | Suppress progress; verdict still prints |

`OLLAMA_HOST` and `OLLAMA_MODEL` are read by `LLMClient` directly
(via `app.core.config`), so set them in the environment before
launching.

### Output

Three files land in `--output-dir` (default `./test_output/`):

- `baseline.json` — exact `SSHCollector.collect()` dump from the source VM
- `current.json`  — exact `SSHCollector.collect()` dump from the target VM
- `verdict.json`  — full LLM response: `status` + `summary` +
  `findings[]` + `remediation[]` + the computed `diff` block

The verdict is also pretty-printed to **stdout**:

```
══════ VALIDATION VERDICT ══════
  status     : DEGRADED  (warn)
  summary    : Postgres is no longer listening on 5432; cron schedules drifted.
  findings   : 2
  remediation: 2 step(s)

── findings ──
  [ 1] [CRITICAL] [services] postgresql.service is not running on the target
  [ 2] [WARN    ] [cron    ] root crontab is empty after migration

── remediation ──
  1. Start postgresql on the target VM
       $ sudo systemctl enable --now postgresql
  2. Restore root's crontab from the baseline
```

### Exit codes

| Code | Meaning |
|------|---------|
| `0`  | Verdict was `pass` (HEALTHY) or `warn` (DEGRADED) |
| `1`  | Verdict was `fail` (FAILED), or any step (SSH / LLM) errored |
| `2`  | Bad arguments, or `app.core` not importable |
| `130`| Interrupted (Ctrl-C) |

Note: a `warn` verdict exits `0` because most pipelines want to
distinguish "needs review" from "broken". Treat the JSON `status`
field as the source of truth when scripting around the script.

### Common failure modes

#### "baseline is missing required keys [...]"

The SSH collector returned a result that's missing one of `meta`,
`services`, `network`, `ports`, `mounts`, `cron`. Run
`scripts/test_ssh.py` against the same VM to surface the underlying
issue (most likely a sudoers or PATH problem on the target).

#### "Cannot reach Ollama at <url>"

Ollama isn't running, or the URL is wrong. Verify:

```bash
podman ps | grep ollama
podman logs $(podman ps -q --filter name=ollama) | tail
curl -sf "$OLLAMA_HOST/api/tags" | head
```

If Ollama is on the same host as the script (not in the podman-compose
network), set `OLLAMA_HOST=http://localhost:11434`.

#### "LLM call exceeded the 120s timeout"

CPU inference of `llama3:8b` can be slow on first call (model needs to
warm up). Either:

- Bump `--llm-timeout 300` (or higher),
- Pre-warm with `podman exec ollama ollama run llama3:8b "hi"`, or
- Switch to a smaller model: `OLLAMA_MODEL=llama3.2:3b`.

#### "Model not pulled yet" / "non-JSON envelope"

The model name in `OLLAMA_MODEL` doesn't exist locally. Pull it:

```bash
podman exec -it $(podman ps -q --filter name=ollama) ollama pull llama3:8b
```

#### "Invalid verdict status"

The model returned something outside `pass|warn|fail`. Usually means
the model is too small to follow the JSON schema reliably; switch back
to `llama3:8b` (or larger) and re-run.
