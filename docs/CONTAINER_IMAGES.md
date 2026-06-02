# Container Images

VirtValidate ships **two variants** of each container image, both
built on Red Hat supply chain:

| Variant | Backend base | Frontend runtime base | Tag suffix |
|---------|---|---|---|
| **Standard** (default) | `registry.access.redhat.com/ubi9/python-312@sha256:...` | `registry.access.redhat.com/ubi9/nginx-124@sha256:...` | none |
| **Hardened** (opt-in) | `registry.redhat.io/<hardened-python>:...` | `registry.redhat.io/<hardened-nginx>:...` | `-hardened` |

Both variants compile from the same source, target the same
deployment surface, and run as UID 1001 / GID 0. The hardened
variant adds Red Hat's image-hardening pipeline (minimal,
SBOM-embedded, signed, near-zero CVE) at the cost of distroless
ergonomics — no shell inside the pod.

The standard variant pins FROM lines to digests rather than `:latest`
so every build is reproducible and Dependabot can surface base-image
updates as PRs.

This document explains why UBI is the only supported base, how the
images are laid out, how to scan them, and how to update versions.

---

## Why UBI is required

Three reasons this product cannot ship on Docker Hub community
images:

1. **Federal / DoD security reviews reject Docker Hub bases.** The
   product's primary customer is federal — DoD, FedRAMP, civilian
   agencies — and every one of them has a "no Docker Hub in
   production" rule. Red Hat UBI is on the approved list.

2. **FIPS 140-3 compliance requires UBI.** The host kernel's FIPS
   module is only certified to pair with Red Hat-built userspace.
   Running `python:3.12-slim` (Debian) on a FIPS-enabled RHEL host
   gives you an OpenSSL inside the container that *isn't* in the
   validated boundary. The product's `FIPS_MODE` flag would be
   meaningless. See `docs/FIPS_DEPLOYMENT.md` for the full chain.

3. **Red Hat partnership commitments.** This product is positioned
   as a Red Hat-aligned tool for vSphere → OpenShift Virtualization
   migration. Shipping community-base containers undercuts the
   partnership story and triggers questions on every customer call.

UBI is freely redistributable (no Red Hat subscription needed to
pull or run), but the supply chain is Red Hat's — every layer is
signed, every CVE backport is published in their advisories.

---

## Image layout

### Backend

```
/opt/app-root/src/             ← UBI canonical workdir + WORKDIR
├── alembic.ini
├── app/                        FastAPI source
├── migrations/                 Alembic versions
├── templates/                  Static assets (CSV inventory template)
└── keys/                       SSH private keys (mode 700, gid 0)

/app  →  /opt/app-root/src      Symlink kept for back-compat with
                                podman-compose / Quadlet / Helm
                                volume mounts that still reference
                                /app/keys.
```

**Runtime user:** UID 1001 / GID 0 (UBI default). OpenShift's
`restricted-v2` SCC assigns an arbitrary high UID with primary
GID 0; chgrp 0 + chmod g=u on every directory the runtime writes
to keeps the SCC happy.

**Installed packages** (via `dnf` from UBI 9 BaseOS + AppStream):

- `openssh-clients` — SSH collector + paramiko
- `pango`, `cairo`, `gdk-pixbuf2`, `shared-mime-info` — weasyprint runtime
- `dejavu-sans-fonts` — weasyprint default font face

### Frontend

Two-stage build:

| Stage | Base | Purpose |
|-------|------|---------|
| `builder` | `ubi9/nodejs-20` | `npm ci` + `npm run build` |
| runtime | `ubi9/nginx-124` | Serves `/opt/app-root/src` on port 8080 |

**Runtime user:** UID 1001 / GID 0.

**Listen port:** 8080 (UBI nginx convention). The host port stays at
3000 via podman-compose port-mapping (`3000:8080`), the Service
publishes on 3000, and the Quadlet `PublishPort=3000:8080` keeps the
operator-facing URL unchanged.

The `nginx.conf` we ship replaces UBI's stock `/etc/nginx/nginx.conf`
wholesale. Earlier versions dropped a config into `conf.d/` which UBI
didn't include — silently breaking proxy and SPA fallback.

---

## Image sizes

Captured at v0.x (UBI 9.7 / Python 3.12 / nginx 1.24):

| Image | Compressed | On-disk |
|-------|-----------:|--------:|
| `virtvalidate-backend` | ~520 MB | **1.42 GB** |
| `virtvalidate-frontend` | ~120 MB | **361 MB** |

These are bigger than the v0.1 Docker Hub bases (Python slim was
~120 MB, Alpine nginx ~50 MB). The size delta is the price of UBI's
broader package set + Red Hat's signed supply chain. Federal
deployments accept the trade-off; lab benches can mirror the UBI
images locally to dodge per-CI pull bandwidth.

---

## Building locally

Two scripts coexist:

```bash
# Single-component, multi-arch, scan-on-build flows (existing):
./scripts/build-images.sh                    # build both, no scan, no push
./scripts/build-images.sh -b backend         # build just the backend
./scripts/build-images.sh -s                 # build + trivy scan
./scripts/build-images.sh -p                 # build + push
./scripts/build-images.sh -p -s -m           # full release pipeline

# Full matrix (standard + hardened, backend + frontend) for a release:
./scripts/build-all-images.sh v0.2.6                       # standard + hardened, no push
./scripts/build-all-images.sh v0.2.6 --push                # standard + hardened, push
./scripts/build-all-images.sh v0.2.6 --push --standard-only # standard only

# Hardened builds via Hummingbird (public registry, no auth):
HARDENED_PYTHON=registry.access.redhat.com/hi/python:3.12-builder \
HARDENED_NGINX=registry.access.redhat.com/hi/nginx:latest-builder \
    ./scripts/build-all-images.sh v0.2.6 --push
```

When `HARDENED_PYTHON` or `HARDENED_NGINX` is empty, the matrix runner
skips that component's hardened build with a `WARN` log and the
standard build still proceeds — so the script works even before the
operator has registered the RHHI service-account paths.

The single-component script preflights a `podman pull` of every UBI
base before building so a connectivity issue surfaces immediately
rather than five minutes into the layer cache.

---

## Updating base image versions

Both variants pin their FROM lines to `@sha256:` digests rather than
floating tags so every build is reproducible. Pinning to digests means
Dependabot opens a PR when Red Hat publishes a new patched image (the
`docker` ecosystem watches the FROM lines).

When Red Hat ships a new minor (e.g., python-312 → python-313, nginx
1.24 → nginx 1.26) OR a new patched digest of the current minor,
update the `FROM` lines in:

- `backend/Containerfile`
- `backend/Containerfile.hardened` (Stage 1 only — the runtime BASE_IMAGE
  comes from the build script)
- `frontend/Containerfile` (both stages)
- `frontend/Containerfile.runtime`
- This document
- The `UBI_BASE_IMAGES` array in `scripts/build-images.sh`
- The `org.opencontainers.image.base.name` label in each Containerfile

Resolve a new digest the same way:

```bash
podman pull registry.access.redhat.com/ubi9/python-312:latest
podman inspect registry.access.redhat.com/ubi9/python-312:latest \
    --format '{{.Digest}}'
# → sha256:...
```

Then rebuild + scan:

```bash
./scripts/build-images.sh -s     # rebuild + scan
podman-compose down
podman-compose up -d --build
podman exec virtvalidation_backend_1 cat /etc/os-release  # verify
```

The integration suite (`backend/tests/`) runs against the new image
in CI. Expect to fix one or two compatibility nicks per UBI minor
bump (paramiko + cryptography are the usual suspects).

---

## Security scanning (optional, local)

> **Note:** Automated CI scanning and dependency-update bots were
> deferred during early development (see CLAUDE.md → "CI & security
> scanning (intentionally deferred)"). There is no scan gate in CI
> today. The local opt-in scan below remains available for developers
> who want it, and the hardened image variant exists for its runtime
> security properties independent of any scanner.

The build script's `-s` flag runs **trivy** (preferred) or **grype**
against the freshly built image and fails the *local* build on
HIGH/CRITICAL findings. It is opt-in — nothing scans automatically.

### Local install

```bash
brew install aquasecurity/trivy/trivy
# or
brew install grype
```

UBI base images carry their own CVE feed (`vuln-listings.redhat.com`)
which trivy consumes automatically — VEX statements published by Red
Hat suppress findings already known to be non-exploitable in the
product.

### What the `-s` flag does *not* scan

- Build-stage layers (`ubi9/nodejs-20`). Those layers don't ship in
  the runtime image, so a CVE in a node devDep doesn't reach
  production. Trivy is invoked against the final tag only.

---

## FIPS implications + verification

UBI is the necessary container layer for FIPS, but **not sufficient
on its own**. The host kernel must be in FIPS mode for the
in-container OpenSSL to operate inside the validated boundary.

To verify the runtime is actually using FIPS-validated crypto:

```bash
# 1. Host kernel
cat /proc/sys/crypto/fips_enabled         # → 1

# 2. Inside the backend container
podman exec virtvalidation_backend_1 \
    python -c "import ssl; print(ssl.OPENSSL_VERSION)"
# → "OpenSSL 3.0.7 1 Nov 2022 (Red Hat Enterprise Linux)"

# 3. VirtValidate FIPS posture
curl -s http://localhost:8000/api/system/fips-status | jq
# → {"fips_mode": true, "host_fips_enabled": true,
#    "ssh_key_algorithm": "rsa-3072", ...}
```

Full requirements + recovery procedures live in
[`docs/FIPS_DEPLOYMENT.md`](./FIPS_DEPLOYMENT.md).

---

## Air-gapped / disconnected deployments

Federal classified labs typically can't reach
`registry.access.redhat.com` or `registry.redhat.io`. Mirror the bases
through a local registry once, then point the build script and the
Helm chart at the mirror.

### Mirroring the UBI standard bases

```bash
# On a connected host, save the bases
for tag in ubi9/python-312:latest ubi9/nodejs-20:latest ubi9/nginx-124:latest; do
    podman pull "registry.access.redhat.com/$tag"
    podman save -o "${tag//\//-}.tar" "registry.access.redhat.com/$tag"
done

# Move to the air-gapped lab, load + retag
for f in *.tar; do podman load -i "$f"; done
```

### Mirroring the hardened bases

The hardened bases live on the authenticated `registry.redhat.io`
endpoint, so the mirror flow runs through `oc image mirror` or
`skopeo copy` with the service-account token:

```bash
# On a connected host (with podman login registry.redhat.io done first)
for path in <hardened-python-path>:<tag> <hardened-nginx-path>:<tag>; do
    skopeo copy \
        docker://registry.redhat.io/${path} \
        docker-archive:./$(basename "$path" | tr ':' '-').tar
done
```

### Mirroring VirtValidate's own images (both variants)

```bash
# Mirror standard + hardened tags side-by-side
for variant in "" "-hardened"; do
    for component in backend frontend; do
        skopeo copy \
            docker://quay.io/cmalafis10/virtvalidate-${component}:v0.2.6${variant} \
            docker://internal.example.com/virtvalidate/virtvalidate-${component}:v0.2.6${variant}
    done
done
```

### Pointing Helm at the mirror

The Helm chart's `image.registry` value is the single override point
for the entire chart — flipping it re-targets backend, frontend, and
migrations job at once:

```bash
helm upgrade virtvalidate ./deploy/helm/virtvalidate \
  -n virtvalidate \
  --set image.registry=internal.example.com/virtvalidate
```

Or via a values override file (preferred for disconnected installs
since the override may include cluster-specific pull secrets):

```yaml
# disconnected-values.yaml
image:
  registry: internal.example.com/virtvalidate
  variant: hardened       # if running the hardened variant
  pullSecrets:
    - internal-mirror-pull
```

```bash
helm install virtvalidate ./deploy/helm/virtvalidate \
  -n virtvalidate \
  -f disconnected-values.yaml
```

Don't republish UBI images on a public registry — Red Hat's UBI EULA
permits redistribution but the OpenContainers labels embed the
`com.redhat.license_terms` URL operators are expected to honor.

---

## registry.redhat.io authentication (hardened variant)

Hardened images pull from `registry.redhat.io`, which requires an
authenticated session. The recommended flow uses a **registry service
account** — operator-bound tokens that can be rotated without
disturbing CI:

1. Sign in at <https://access.redhat.com/terms-based-registry/>
   and create a new service account. Note the username (`<NNN>|<name>`
   form) and the token.
2. **Local Mac** (for `scripts/build-all-images.sh`):
   ```bash
   podman login registry.redhat.io
   # Username: <NNN>|<name>
   # Password: <token>
   ```
   The hardened base catalog paths are passed to the build script via
   the `HARDENED_PYTHON` / `HARDENED_NGINX` env vars (see
   `scripts/build-all-images.sh`). There is no CI build path today —
   hardened images are built locally and pushed from the Mac.
3. **OpenShift cluster** (for pulling the hardened images at deploy
   time):
   ```bash
   oc -n virtvalidate create secret docker-registry redhat-registry-pull \
       --docker-server=registry.redhat.io \
       --docker-username='<NNN>|<name>' \
       --docker-password='<token>'
   ```
   Reference it in the Helm chart:
   ```yaml
   image:
     variant: hardened
     pullSecrets:
       - redhat-registry-pull
   ```

The Postgres "useRedHatImage" path documented further down uses the
same pull-secret recipe — one secret can serve both.

---

## Database container — Postgres on UBI

The Helm chart ships two database images and switches between them
via `postgres.image.useRedHatImage` in values.yaml:

| Option | Image | Auth | Why pick it |
|--------|-------|------|-------------|
| Default | `docker.io/library/postgres:16-alpine` | none | Works out of the box; runs on OCP only with the `anyuid` SCC because the upstream entrypoint chowns the data dir as root. |
| Federal | `registry.redhat.io/rhel9/postgresql-16` | required | FIPS-validated OpenSSL, runs cleanly under OCP restricted-v2 (arbitrary UID), passes federal customer security review. |

**The two images aren't drop-in compatible.** The Red Hat image
uses different env var names (`POSTGRESQL_USER` / `POSTGRESQL_PASSWORD`
/ `POSTGRESQL_DATABASE` — with the QL) and writes data to
`/var/lib/pgsql/data` (not `/var/lib/postgresql/data`). The chart
handles both: when `useRedHatImage: true`, the statefulset renders
`POSTGRESQL_*` env aliases pointing at the same Secret keys plus
the matching mountPath.

### Pull secret for registry.redhat.io

`registry.redhat.io` requires authentication; the public mirror
`registry.access.redhat.com` does NOT publish `postgresql-16`, so
the federal path mandates a pull secret. Two ways:

```bash
# 1) From an existing Red Hat customer portal account:
oc -n virtvalidate create secret docker-registry redhat-pull-secret \
    --docker-server=registry.redhat.io \
    --docker-username='<your username>' \
    --docker-password='<your token>'

# 2) From a Service Account token (preferred for production):
oc -n virtvalidate apply -f /path/to/redhat-pull-secret.yaml

# Reference it from values.yaml:
imagePullSecrets:
  - name: redhat-pull-secret

# Bump postgres image:
postgres:
  image:
    useRedHatImage: true
    registry: registry.redhat.io
    repository: rhel9/postgresql-16
    tag: "latest"
```

If your customer's environment forbids `registry.redhat.io`, mirror
`rhel9/postgresql-16` into an internal registry first using
`oc image mirror`; reuse the same Pull Secret pattern pointing at
your internal mirror. See `docs/FIPS_DEPLOYMENT.md` for the full
mirroring procedure used by the air-gapped reference deployment.

---

## Related docs

- [`docs/FIPS_DEPLOYMENT.md`](./FIPS_DEPLOYMENT.md) — FIPS chain of custody.
- [`docs/INSTALLATION.md`](./INSTALLATION.md) — operator-facing install.
- [`docs/DEPLOYMENT_TROUBLESHOOTING.md`](./DEPLOYMENT_TROUBLESHOOTING.md) — live-debug log from the first OCP deployment.
- [`deploy/README.md`](../deploy/README.md) — Helm + Kustomize deployment.
- `backend/Containerfile` and `frontend/Containerfile` — the source of truth.
