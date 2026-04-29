# Contributing to VirtValidate

Thanks for your interest in VirtValidate. This project validates VMs migrated
from VMware to OpenShift Virtualization, with all reasoning done locally —
contributions that keep it air-gapped, reproducible, and useful in regulated
environments are especially welcome.

This document covers how to set up a dev environment, run tests, the code
style we expect, and the PR process (including the DCO sign-off).

## Code of Conduct

This project adheres to the [Contributor Covenant 2.1](CODE_OF_CONDUCT.md).
By participating, you agree to abide by its terms. Report unacceptable
behavior to the maintainers (see [SECURITY.md](SECURITY.md) for contact).

## Development setup

### Prerequisites

- Podman 4.4+ (rootless) and `podman-compose` 1.0.6+
- Python 3.12
- Node.js 20+ (for the frontend)
- Git
- 16 GB RAM, 30 GB free disk

### Clone and bootstrap

```bash
git clone https://github.com/cmalafis/virtvalidation.git
cd virtvalidation

# Backend
cd backend
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cd ..

# Frontend
cd frontend
npm install
cd ..

# Pre-commit hooks (ruff, mypy)
pip install pre-commit
pre-commit install
```

### Run the stack

```bash
cp .env.example .env
$EDITOR .env

mkdir -p backend/app/keys
ssh-keygen -t ed25519 -N "" -f backend/app/keys/id_ed25519 \
    -C "virtvalidate@dev"

podman-compose up -d ollama
podman-compose exec ollama ollama pull llama3:8b
podman-compose up -d
```

The dashboard is at `http://localhost:3000` and the API at
`http://localhost:8000` (`/docs` for Swagger UI).

### Running components individually (faster iteration)

```bash
# Backend with hot-reload
cd backend
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

# Frontend with hot-reload
cd frontend
npm run dev
```

## Running tests locally

All backend tests live under `backend/tests/` and use pytest.

```bash
cd backend
pytest                          # full suite
pytest tests/test_ssh.py        # one file
pytest -k "wave_planner"        # by keyword
pytest --cov=app --cov-report=term-missing
```

CI enforces 70% coverage on the backend (`--cov-fail-under=70`). New code
should not lower the overall coverage percentage.

Frontend currently has no automated test suite. If you add UI behavior
that warrants tests, please propose the framework choice in your PR.

## Code style

### Python (backend)

We use [Ruff](https://docs.astral.sh/ruff/) for lint + format and
[mypy](https://mypy-lang.org/) for type checking. Both are pinned in
`backend/requirements.txt` and configured in `backend/pyproject.toml`.

```bash
cd backend
ruff check .            # lint
ruff format .           # auto-format
ruff format --check .   # CI check
mypy app                # type-check
```

Conventions:

- Line length 100, target Python 3.12.
- Type-annotate new public functions; existing untyped code is being
  tightened incrementally — don't regress what's already typed.
- First-party imports are `app` and `tests` (configured in isort).
- Use `httpx` for HTTP, `paramiko` for SSH, `sqlalchemy` 2.x style.
- LLM calls go to Ollama at `$OLLAMA_HOST` only — **never** add a
  dependency that calls an external API. The platform is air-gapped by
  design.

### JavaScript / React (frontend)

- React 18 functional components + hooks.
- Tailwind for styling — prefer utility classes over new CSS files.
- Keep components small; extract hooks for shared state.
- No external analytics, telemetry, or CDN-loaded assets.

### Containers

- Use `Containerfile` (not `Dockerfile`) and Podman conventions.
- Image refs must be fully qualified (`docker.io/...`,
  `registry.access.redhat.com/...`) — Podman does not auto-resolve.
- Volume mounts use the `:Z` SELinux label.

### Commits

- Follow [Conventional Commits](https://www.conventionalcommits.org/) where
  practical: `feat:`, `fix:`, `docs:`, `chore:`, `refactor:`, `test:`.
- Keep the subject under 72 characters; use the body for the *why*.
- One logical change per commit; rebase noisy WIP commits before pushing.

## Pull request process

1. **Fork** the repo and create a feature branch from `main`:
   `git checkout -b feat/short-description`.
2. **Write tests** for any new behavior. Bug fixes should include a
   regression test.
3. **Run the full check locally** before pushing:
   ```bash
   cd backend
   ruff check . && ruff format --check . && mypy app && pytest
   ```
4. **Sign your commits** (see DCO below).
5. **Open the PR** against `main`. Fill out the PR template — what
   changed, why, and how it was tested.
6. **CI must pass.** The workflow runs ruff, mypy, and pytest with
   coverage on every PR.
7. **Address review feedback** by pushing additional commits (don't
   force-push during review unless asked — it makes incremental review
   harder). Squash/rebase before merge if requested.
8. **Merge.** Maintainers will squash-merge once approved and CI is
   green.

### What makes a PR easy to merge

- Scope is one thing. If you find yourself writing "and also…" in the
  description, split it.
- Description explains *why*, not just *what* — the diff already shows
  what changed.
- Tests demonstrate the behavior. Reviewers shouldn't have to take your
  word that the fix works.
- No unrelated formatting churn. Run the formatter on your changes
  only, not the whole tree.

## Developer Certificate of Origin (DCO)

We use the [Developer Certificate of Origin](https://developercertificate.org/)
instead of a CLA. Every commit must be signed off, certifying that you
wrote the code (or otherwise have the right to contribute it under the
project's license).

Add `Signed-off-by: Your Name <your.email@example.com>` to each commit:

```bash
git commit -s -m "fix: handle empty inventory response"
```

The `-s` flag appends the trailer using your configured
`user.name` / `user.email`. Set those once per machine:

```bash
git config --global user.name  "Your Name"
git config --global user.email "your.email@example.com"
```

The full DCO text:

```
Developer Certificate of Origin
Version 1.1

By making a contribution to this project, I certify that:

(a) The contribution was created in whole or in part by me and I have
    the right to submit it under the open source license indicated in
    the file; or

(b) The contribution is based upon previous work that, to the best of
    my knowledge, is covered under an appropriate open source license
    and I have the right under that license to submit that work with
    modifications, whether created in whole or in part by me, under
    the same open source license (unless I am permitted to submit
    under a different license), as indicated in the file; or

(c) The contribution was provided directly to me by some other person
    who certified (a), (b) or (c) and I have not modified it.

(d) I understand and agree that this project and the contribution are
    public and that a record of the contribution (including all
    personal information I submit with it, including my sign-off) is
    maintained indefinitely and may be redistributed consistent with
    this project or the open source license(s) involved.
```

PRs without sign-offs will be asked to amend before merge:

```bash
git rebase --signoff main
git push --force-with-lease
```

## Reporting bugs and requesting features

- **Bugs:** open an issue using the bug report template. Include
  Podman version, OS, the failing command, and logs.
- **Features:** open a feature request issue first to discuss scope
  before sending a large PR.
- **Security:** do **not** open a public issue. See
  [SECURITY.md](SECURITY.md).

## Questions

Open a GitHub Discussion or issue with the `question` label. We try to
respond within a few business days.
