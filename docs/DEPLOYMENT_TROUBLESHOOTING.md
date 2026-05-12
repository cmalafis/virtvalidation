# Deployment Troubleshooting

Live-debug notes from the first VirtValidate deployment to a real
OpenShift cluster (Red Hat Demo Platform with RHOAI). Every fix
below was applied on-cluster, then committed back as a chart /
image change so future installs work out of the box. Symptom →
root cause → fix → prevention for each issue.

---

## 1. Backend can't authenticate to Postgres

### Symptom
```
FATAL: password authentication failed for user "$(POSTGRES_USER)"
```
The literal string `$(POSTGRES_USER)` shows up as the username in
Postgres logs. The backend pod CrashLoopBackOffs on the first
migration attempt.

### Root cause
Kubernetes env var substitution (`$(VAR)`) only works on env vars
defined **earlier** in the same container's `env:` list. If
`DATABASE_URL` is declared before `POSTGRES_USER`, kubelet has
nothing to substitute, so the placeholder survives intact and gets
passed to libpq as the literal username.

### Fix
`deploy/helm/virtvalidate/templates/deployment-backend.yaml`:
declare `POSTGRES_USER` and `POSTGRES_PASSWORD` FIRST, then
`DATABASE_URL` second. An inline comment in the template flags
this so editors don't reshuffle the order out of style preference.

### Prevention
- The chart now has the right order, with a comment explaining
  why.
- A future-CI check could `helm template`-render and grep the
  `env:` block to make sure `POSTGRES_USER` precedes `DATABASE_URL`.
  Not implemented today; the comment is the primary guard.

---

## 2. Frontend nginx can't reach backend

### Symptom
```
[error] connect() failed (111: Connection refused) while connecting
to upstream, client: 10.x.x.x, upstream: "http://backend:8000/..."
```
The `Settings` panel can't load `/api/health/llm`; every request
404s through the frontend pod.

### Root cause
`frontend/nginx.conf` used the literal hostname `backend:8000`,
which works in `podman-compose` (where the service is named
`backend`) but breaks on OpenShift where the Helm-rendered Service
is `<release>-backend` (e.g. `virtvalidate-backend`).

### Fix
- `frontend/nginx.conf.template` (renamed from `.conf`) uses
  `${BACKEND_HOST}:${BACKEND_PORT}` placeholders.
- `frontend/entrypoint.sh` runs `envsubst` at container start to
  render the final `nginx.conf`.
- `deploy/helm/.../deployment-frontend.yaml` sets `BACKEND_HOST` to
  the release-prefixed Service name; `podman-compose.yml` sets
  `BACKEND_HOST=backend` for dev parity.

### Prevention
- The two env vars are documented in `docs/INSTALLATION.md` and in
  the template header comment.
- Override via the chart: `--set frontend.extraEnv[...]` works for
  cluster setups where the Service is named differently.

---

## 3. nginx fails to start with "permission denied" on /tmp

### Symptom
```
nginx: [emerg] open() "/tmp/nginx/proxy/0/00/0000000001" failed
(2: No such file or directory)
```
or
```
[crit] mkdir() "/var/cache/nginx/client_temp" failed (13: Permission
denied)
```
Frontend pod CrashLoopBackOffs on its first request.

### Root cause
The Helm chart mounts `/var/lib/nginx`, `/run`, and `/tmp` as
`emptyDir` volumes — required for OpenShift's arbitrary-UID
security context. Those mounts **overlay** any subdirectories baked
into the image, so the pre-created `/tmp/nginx/{client_body,proxy,…}`
layout is invisible at runtime.

### Fix
`frontend/entrypoint.sh` runs `mkdir -p /tmp/nginx/{client_body,
proxy,fastcgi,uwsgi,scgi}` **after** the mount, then exec's nginx.
The pod's writable `/tmp` is the only safe surface for these on
OCP restricted-v2.

### Prevention
- The entrypoint is the entry point baked into the image (`CMD
  ["/usr/local/bin/entrypoint.sh"]`); the chart can't accidentally
  skip it.
- The nginx.conf.template comment block calls out that all
  `*_temp_path` directives must stay under `/tmp/nginx/`.

---

## 4. Cross-architecture image builds fail on Apple Silicon

### Symptom
```
panic: runtime error: invalid memory address or nil pointer dereference
[signal SIGSEGV: segmentation violation code=0x1 addr=0x0 pc=...]
```
Buried in the `npm run build` output during `podman build
--platform linux/amd64 -f frontend/Containerfile`. The crash is
inside `node_modules/@esbuild/linux-x64/bin/esbuild` running
through QEMU x86_64-on-arm64 emulation.

### Root cause
esbuild ships native binaries per platform. Under QEMU's
user-mode emulation, the amd64 esbuild binary occasionally
segfaults on Apple Silicon hosts. The crash is not deterministic
but reproduces often enough to break CI-on-a-laptop loops.

### Fix
`scripts/build-images.sh` detects the cross-arch case
(host=arm64, target=linux/amd64) and switches to a two-step path:

1. Run `npm ci && npm run build` natively on the host (no
   emulation).
2. Build the image with `frontend/Containerfile.runtime`, which
   only carries nginx + the pre-built `dist/`.

Output image is byte-identical to the multi-stage build's output
— just produced without the QEMU leg.

### Prevention
- The script handles this automatically; the only operator
  burden is having Node 20+ installed locally.
- Override with `PLATFORM=linux/arm64` to build natively for
  M-series testing.

---

## 5. Helm + kubectl field manager conflicts

### Symptom
```
Error: UPGRADE FAILED: cannot patch "virtvalidate-backend"
with kind Deployment: Operation cannot be fulfilled on
deployments.apps "virtvalidate-backend": the object has been
modified
```
…appearing after the operator ran `oc patch` to fix one of the
issues above, then tried `helm upgrade`.

### Root cause
`oc patch` / `kubectl edit` register their changes under a
different field manager than Helm. Helm's three-way merge no
longer "owns" the patched fields, so it can't reapply them. The
shell error is a kubernetes-server-side-apply conflict, not a
Helm bug.

### Fix
- Don't `oc patch` on a chart-managed object — fix the chart and
  `helm upgrade` instead.
- If a live patch is unavoidable, follow with
  `helm upgrade --force` or
  `kubectl apply -f <(helm template …) --force-conflicts
  --server-side` to reclaim ownership.

### Prevention
- All the live patches that triggered this debugging session are
  now in the committed chart — no operator should need to
  `oc patch` to bring the pod up.
- If you find yourself reaching for `oc patch` during a deploy,
  the right next step is a chart fix + PR.

---

## 6. Postgres pod stuck in CrashLoopBackOff with permission error

### Symptom
```
chmod: changing permissions of '/var/lib/postgresql/data': Operation not permitted
```
The `postgres:16-alpine` container exits 1 during startup on a
cluster with a strict-FIPS / restricted-v2 SCC.

### Root cause
The upstream `postgres` image expects to run its entrypoint as
root (uid 0) to chown the data dir, then drop to the `postgres`
user. OCP restricted-v2 assigns an arbitrary UID, so the chown
step fails.

### Fix
Two paths:
1. **Stay on the Docker Hub image** (default): grant the workload
   the `anyuid` SCC. Documented but not preferred for federal
   deployments.
2. **Switch to Red Hat postgresql-16**: set
   `postgres.image.useRedHatImage: true` in values + point the
   registry/repository/tag at `registry.redhat.io/rhel9/postgresql-16`.
   The chart renders matching `POSTGRESQL_*` env aliases + the
   `/var/lib/pgsql/data` mount path. Requires a pull secret —
   see `docs/CONTAINER_IMAGES.md`.

### Prevention
- The chart now supports both images via a single flag — no
  template surgery needed.
- `helm template` after enabling the flag should show the
  POSTGRESQL_USER alias before the mountPath block.

---

## 7. Helm upgrade leaves stale env vars

### Symptom
After bumping the chart from 0.1.0 → 0.1.1 with reshuffled env
vars, the running pod still showed the old env var order in
`oc describe pod`.

### Root cause
Helm tracks the rendered manifest, not the rendered env array
specifically. When the order changes but the values don't,
kubectl's strategic merge can fail to recognize the order change
as a meaningful diff if the resource is patched with `apply`
instead of `replace`. The pod stayed running with the old
template.

### Fix
- The deployment-backend.yaml carries a `checksum/config`
  annotation that includes the rendered ConfigMap hash. Changes
  to env ordering don't bump the hash on their own; if you need
  to force a rollout after a template-only change, bump the
  chart version (which we did: 0.1.0 → 0.1.1).
- `oc rollout restart deployment/virtvalidate-backend` forces a
  refresh of running pods using the latest template.

### Prevention
- Always bump `Chart.yaml.version` when changing templates, even
  if `values.yaml` defaults are unchanged.
- Roll out manually after template-only changes:
  `helm upgrade … && oc rollout restart deployment/<...>-backend`.

---

## Verification checklist

After applying any of the fixes above, run:

```bash
# 1. Lint the chart for syntax issues
helm lint deploy/helm/virtvalidate/

# 2. Render templates and inspect them
helm template virtvalidate deploy/helm/virtvalidate/ \
  --set image.registry=quay.io/your-registry \
  --set image.tag=0.1.1 \
  > /tmp/rendered.yaml

# 3. Confirm env var ordering in backend deployment
grep -A 30 'name: backend' /tmp/rendered.yaml | grep -m2 -E 'POSTGRES_USER|DATABASE_URL'
# Expected: POSTGRES_USER appears before DATABASE_URL

# 4. Confirm BACKEND_HOST in frontend deployment
grep -B 1 'BACKEND_HOST' /tmp/rendered.yaml
# Expected: name: BACKEND_HOST / value: virtvalidate-backend (or your release)

# 5. Smoke-build the images
./scripts/build-images.sh -v 0.1.1-test
# Expected: both images build cleanly on any host arch
```

If `helm install` still hangs or produces CrashLoopBackOff pods
after these checks pass, capture `oc get events --sort-by=.lastTimestamp`
and add the symptom + root cause + fix as a new section here.
