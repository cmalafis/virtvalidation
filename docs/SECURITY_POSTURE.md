# VirtValidate Security Posture

This document summarizes VirtValidate's security posture for
procurement, compliance, and customer audit reviews. It complements
[`SECURITY.md`](../SECURITY.md) (responsible disclosure policy) and
[`docs/CONTAINER_IMAGES.md`](./CONTAINER_IMAGES.md) (image build +
mirror procedures).

> **TL;DR.** VirtValidate ships a hardened, distroless, signed,
> near-zero-CVE image variant deployable with a single Helm flag.
> All AI inference is local; the product never calls external APIs.
> CI runs four security scanners with results published as SARIF.

---

## Threat model

### What VirtValidate is

A read-only appliance that:

1. **SSHes into source VMware VMs** to collect a multi-day baseline of
   services, network state, mounts, and configuration.
2. **SSHes into post-migration OCP-Virt VMs** to validate that state
   matches the baseline.
3. **Generates MTV (Forklift) Plan / NetworkMap / StorageMap YAML** so
   operators can apply migrations from a single bundle.
4. **Runs a local LLM** (Ollama / vLLM / KServe) for migration
   planning and validation reasoning.

### What VirtValidate is NOT

- It does **not** modify source or target VMs (read-only SSH).
- It does **not** call vCenter, OCP-Virt, or any cloud API.
- It does **not** send any data outbound. AI inference runs locally.
- It does **not** persist long-term credentials beyond an Ed25519
  SSH private key in a PVC-backed `/app/keys` directory.

### Trust boundaries

```
┌──────────────────┐     SSH      ┌─────────────────────┐     SSH     ┌──────────────────┐
│  Source VMs      │ ◄─────────── │  VirtValidate       │ ──────────► │  Target VMs      │
│  (VMware)        │   Ed25519    │  appliance          │  Ed25519    │  (OCP-Virt)      │
└──────────────────┘              │                     │             └──────────────────┘
                                  │  Backend (Python)   │
   ┌──────────────────────────────│  Frontend (nginx)   │
   │                              │  PostgreSQL         │
   │                              │  LLM (Ollama/vLLM)  │
   ▼                              └─────────────────────┘
┌──────────────────┐                          ▲
│  Operator        │   HTTPS via OCP Route    │
│  browser         │ ─────────────────────────┘
└──────────────────┘
```

The appliance never originates traffic to the public Internet. SSH
egress to managed VMs is the only network boundary the product
crosses. All other network paths are internal: Service-to-Service
DNS within a namespace.

### Authentication boundaries

| Boundary | Mechanism | Notes |
|---|---|---|
| Operator → frontend | OCP Route + optional OAuth2 proxy | Out of chart scope; deploy in front. |
| Frontend → backend | Service DNS; in-cluster only | nginx `/api/` proxy. |
| Backend → SSH targets | Ed25519 keys, mode 700 | Keys stored in `/app/keys` PVC. Never baked into images. |
| Backend → Postgres | Per-namespace Secret | `POSTGRES_USER` / `POSTGRES_PASSWORD` from Helm-managed or external Secret. |
| Backend → LLM | Service DNS or operator-supplied endpoint | KServe + vLLM accept a bearer token via env. |

VirtValidate intentionally does **not** hold vCenter, OCP, or cloud
credentials. The product runs as a customer-side appliance, not a
control-plane agent.

---

## Dual-variant container image strategy

> **Procurement-relevant.** VirtValidate ships a hardened,
> distroless, signed, near-zero-CVE image variant deployable with a
> single Helm flag. Switch with `--set image.variant=hardened`.

| Variant | Base | Size | CVE posture | Distroless? | Default? |
|---|---|---|---|---|---|
| **Standard** | UBI 9 (`registry.access.redhat.com/ubi9/*`) | ~1.45 GB backend / ~339 MB frontend | Patched on every Red Hat errata; pinned to specific digests | No (has shell, package manager) | Yes |
| **Hardened** | Project Hummingbird (`registry.access.redhat.com/hi/*`, public — no pull secret needed) | ~574 MB backend (~60% smaller) / ~127 MB frontend (~62% smaller) | Hardened-image distribution, narrow signed package set | Mostly — minimal package set; has shell + microdnf but no graphics stack (cairo/pango/gdk-pixbuf are deliberately absent) | Opt-in |

Both variants are built from identical source. The hardened backend
adds a multi-stage wheel build so weasyprint media libraries are
installed via `microdnf` in the hardened runtime — preserving the
PDF-rendering and SSH-collector code paths.

### Switching between variants

```bash
# Default deploy (standard UBI)
helm install virtvalidate ./deploy/helm/virtvalidate -n virtvalidate

# Hardened deploy (Hummingbird bases). registry.access.redhat.com is
# public — no pull secret required.
helm install virtvalidate ./deploy/helm/virtvalidate -n virtvalidate \
  --set image.variant=hardened

# Switch a running deployment without a full helm upgrade
oc set image deployment/virtvalidate-backend -n virtvalidate \
  backend=quay.io/cmalafis10/virtvalidate-backend:<tag>-hardened
oc set image deployment/virtvalidate-frontend -n virtvalidate \
  frontend=quay.io/cmalafis10/virtvalidate-frontend:<tag>-hardened
```

### Known limitations of the hardened variant

- **PDF rendering uses xhtml2pdf (pure Python).** Earlier in the
  dual-variant work, PDF generation was blocked on the hardened
  backend because Project Hummingbird's repo does not ship the
  weasyprint runtime libs (pango / cairo / gdk-pixbuf2). PR #12
  swapped weasyprint for xhtml2pdf + ReportLab, which has no
  native graphics dependencies. Both standard and hardened
  backend variants now render identical PDFs from the same code
  path, and the standard backend image shrunk too (the
  `dnf install` line dropped from six packages to one).
- **Static nginx config.** The hardened frontend image cannot run
  the standard variant's `envsubst`-based templating. The
  `/api/` upstream is wired to `virtvalidate-backend:8000`, which
  means the Helm release MUST be named `virtvalidate` (or
  `fullnameOverride: virtvalidate` must be set in values).
- **No interactive shell on hardened nginx pod.** `oc exec` works
  but the shell is busybox-equivalent. Use `kubectl debug` with an
  ephemeral container for deep troubleshooting.
- **Local dev uses standard.** `podman-compose` development still
  pulls the standard images for iteration ergonomics. Hardened is
  for production deploys, not the dev loop.

See [`docs/CONTAINER_IMAGES.md`](./CONTAINER_IMAGES.md) for the
service-account token recipe and air-gapped mirroring procedure for
both variants.

---

## Scanner inventory

| Scanner | What it scans | Cadence | Gating | Where findings surface |
|---|---|---|---|---|
| **gitleaks** | Repo content for secrets (API keys, tokens, certs) | Pre-commit hook + every PR/push | **Blocking** | CI job + local pre-commit |
| **GitHub secret scanning** | Repo + push protection | Continuous | **Blocking** (push protection enforces) | GitHub Security tab |
| **Dependabot** | pip, npm, GitHub Actions, docker FROM lines | Weekly | Surface | PRs |
| **Snyk Open Source (SCA)** | `requirements.txt` + `package.json` dependency CVEs | Push/PR + weekly | Surface only (Phase 1) | GitHub Security tab (SARIF) |
| **Snyk Code (SAST)** | Repo source for code-level vulns | Push/PR | Surface only (Phase 1) | GitHub Security tab (SARIF) |
| **Trivy** | Built image layers (both variants) | Push to main + PR (standard only on PR) | Surface only (Phase 1) | GitHub Security tab (SARIF) |
| **Quay Clair / Container Security Operator** | Pushed images in Quay | Continuous | Surface (Quay UI) | Quay tag CVE column |

**Phase 1** posture is intentional: surface findings to triage them
into the dep pin list rather than failing builds on findings nobody
has classified yet. The hardened variant lets us prove the CVE delta
empirically — once a baseline is committed, Trivy + Snyk gates flip
to blocking.

The CVE delta between standard and hardened is visible in the
Security tab via SARIF categories: `trivy-backend-standard` vs
`trivy-backend-hardened`, same for frontend.

---

## Compliance posture

VirtValidate is **designed against** these frameworks. Customer-side
ATO/RMF packages are the operator's responsibility; we provide the
controls inventory.

### NIST SP 800-53 controls implemented

| Family | Controls | How VirtValidate implements |
|---|---|---|
| **SC-8** Transmission integrity | SC-8(1) | SSH (Ed25519) for all VM access; HTTPS via OCP Route for operator browser. |
| **SC-13** Cryptographic protection | SC-13 | FIPS-validated OpenSSL via UBI base + RHEL host kernel. SSH key algorithm is Ed25519 by default; RSA-3072 / ECDSA-P-384 supported. |
| **SC-28** Protection at rest | SC-28 | Postgres data on PVC (operator-side encryption at the StorageClass layer); SSH keys mode 700 + group 0 in a dedicated PVC. |
| **SI-2** Flaw remediation | SI-2(2), SI-2(3) | Dependabot weekly; Trivy + Snyk SARIF surfacing; CVE delta between standard + hardened variants tracked. |
| **SI-7** Software integrity | SI-7(7) | Hardened variant uses signed, SBOM-embedded Red Hat Hardened Images. Standard variant uses digest-pinned UBI 9. |
| **IA-2** Identification + authentication | IA-2 | Ed25519 SSH keys with operator-side distribution. No password-based VM auth. |
| **AC-6** Least privilege | AC-6 | All containers run as UID 1001 / GID 0 under OpenShift restricted-v2 SCC. Backend has no `NET_ADMIN`, no `SYS_PTRACE`. SSH key directory mode 700. |
| **AU-2 / AU-3** Auditing | AU-2 | Per-VM SSH session logs in `/app/keys/audit.log`; full audit trail in the Postgres database. |
| **CM-2** Baseline configuration | CM-2 | The product's _entire purpose_ is capturing and validating VM baselines. |
| **CM-7** Least functionality | CM-7 | Hardened variant is distroless: no shell, no package manager, no curl. |

### Target operational environments

- **NIST SP 800-53 Rev 5** — applicable controls above.
- **FedRAMP Moderate** — designed against; ATO is customer-side.
- **DoD Impact Level 4 / 5** — the air-gapped, no-external-API
  posture is the design constraint.
- **STIG-compliant deployments** — the UBI 9 base inherits Red Hat
  STIG profiles; the chart's `security.podSecurityContext` /
  `containerSecurityContext` values default to STIG-aligned settings.

### What VirtValidate does NOT claim

- No third-party security certification (FedRAMP authorized,
  StateRAMP, etc.) — those are operator-side accreditations against
  a specific deployment.
- No SOC 2 / ISO 27001 — VirtValidate is software, not a service.
- The FIPS-validated posture requires the **host kernel** to be in
  FIPS mode. UBI alone is not enough. See
  [`docs/FIPS_DEPLOYMENT.md`](./FIPS_DEPLOYMENT.md).

---

## Reporting a security issue

See [`SECURITY.md`](../SECURITY.md). Use GitHub Private Vulnerability
Reporting or the security@ alias documented there. Do not file a
public issue for security reports.

---

## Related documents

- [`docs/CONTAINER_IMAGES.md`](./CONTAINER_IMAGES.md) — image build,
  digest pinning, registry auth, air-gapped mirroring.
- [`docs/FIPS_DEPLOYMENT.md`](./FIPS_DEPLOYMENT.md) — FIPS chain of
  custody.
- [`docs/DEPLOYMENT_TROUBLESHOOTING.md`](./DEPLOYMENT_TROUBLESHOOTING.md)
  — live-debug log from the first production deployment.
- [`SECURITY.md`](../SECURITY.md) — responsible disclosure policy.
