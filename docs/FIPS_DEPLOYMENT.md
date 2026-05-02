# FIPS 140-3 Compliant Deployment

VirtValidate supports deployment in FIPS-enabled environments required
by federal customers (DoD, civilian agencies, FedRAMP). Federal
compliance has two layers: the **host OS** must boot in FIPS mode so
the OpenSSL libraries run inside the validated boundary, and the
**application** must enforce FIPS-only configuration choices on top.
This document covers both, and the gaps where compliance is the
operator's responsibility.

---

## Compliance posture model

| Layer | What | Who owns it |
|-------|------|-------------|
| Host OS kernel + OpenSSL | FIPS 140-3 validated module set | Operator (RHEL / RHCOS install) |
| Container base image | Compatible with the host's FIPS module set | Operator (UBI base image swap) |
| Application crypto choices | SSH algos, TLS verify, hash families | VirtValidate (`FIPS_MODE` flag) |
| Storage encryption | At-rest data on FIPS-encrypted volumes | Operator (LUKS / CSI driver) |
| Network policies | Egress / ingress restrictions | Operator (NetworkPolicy / firewall) |

VirtValidate's job is the **third row** — every line of crypto code in
the application defers to OpenSSL via the `cryptography` and `paramiko`
libraries. The application enforces FIPS-only choices when
`FIPS_MODE=true` and reports its posture so federal reviewers can audit
the configuration.

---

## Requirements

### 1. Host OS in FIPS mode

The kernel must expose `/proc/sys/crypto/fips_enabled` with value `1`.

**RHEL 8 / 9:**
```bash
sudo fips-mode-setup --enable
sudo reboot
cat /proc/sys/crypto/fips_enabled  # → 1
```

**RHCOS (OpenShift):** Set `fips: true` in `install-config.yaml` *before*
running the OpenShift installer. FIPS mode cannot be retrofitted onto
an existing cluster — the cluster must be installed FIPS-from-day-one.

**Other distros:** Refer to your vendor's FIPS module documentation.
Ubuntu Pro FIPS, Debian + libssl-fips, and SLES FIPS modules all work
the same way: kernel mode + validated OpenSSL.

### 2. Container base image

Swap the default `docker.io/python:3.12-slim` base for a UBI image so
the container's OpenSSL matches the host's validated module:

```dockerfile
FROM registry.access.redhat.com/ubi9/python-312:latest
```

UBI alone does **not** make the deployment compliant — the host OS in
step 1 is what activates the validated module. Without step 1 in
place, UBI is just another Python distribution.

### 3. Application FIPS mode

Set the env var in your deployment:
```bash
LLM_BACKEND_TYPE=kserve   # see below — Ollama is non-validated
FIPS_MODE=true
SSH_KEY_ALGORITHM=rsa-3072
```

This activates application-level gates:
- SSH key loader rejects Ed25519 keys, RSA < 3072 bits, and unrecognized algorithms
- LLM backend health probe surfaces the `verify_ssl` configuration as part of compliance reporting
- Startup log line records the configured + detected posture
- `/api/system/fips-status` and `/api/health/full` expose the posture for federal-reviewer probes

### 4. SSH keys generated as RSA-3072 or ECDSA P-384

Replace the default Ed25519 key with a FIPS-approved algorithm:

```bash
# RSA-3072 (FIPS 186-5 minimum)
ssh-keygen -t rsa -b 3072 -f /app/keys/id_ed25519 -N ""

# ECDSA P-384 (FIPS 186-5 approved)
ssh-keygen -t ecdsa -b 384 -f /app/keys/id_ed25519 -N ""
```

The file path is unchanged for back-compat — `SSH_KEY_PATH` defaults
to `/app/keys/id_ed25519` regardless of the actual algorithm. The
loader auto-detects key type and the FIPS gate validates the
algorithm + size at every connection.

> **Note on Ed25519:** Ed25519 is **not** FIPS-approved under
> FIPS 186-5. NIST is still finalizing its standardization for similar
> uses. Until that revision lands, federal deployments must use
> RSA-3072+ or ECDSA P-256/P-384/P-521. VirtValidate accepts Ed25519
> outside FIPS mode and rejects it inside.

### 5. PostgreSQL TLS

The application doesn't configure Postgres — that's the operator's
responsibility. For FIPS deployments, edit `postgresql.conf`:

```ini
ssl = on
ssl_ciphers = 'TLSv1.2:!aNULL:!eNULL:!EXPORT:!DES:!RC4:!MD5'
ssl_min_protocol_version = 'TLSv1.2'
```

Update `DATABASE_URL` to require SSL:
```bash
DATABASE_URL=postgresql+psycopg2://vv:secret@db:5432/vv?sslmode=require
```

### 6. At-rest encryption

PostgreSQL data files and SSH keys must live on FIPS-encrypted storage:

- **Bare metal / VM:** LUKS with `aes-xts-plain64` (FIPS-approved cipher).
- **OpenShift:** Use a CSI driver with encryption support
  (`StorageClass` parameter `csi.storage.k8s.io/encryption: "true"`)
  backed by a KMIP- or Vault-backed key manager.

### 7. LLM backend choice

The default Ollama backend is **not** FIPS-validated for federal
deployments. For full compliance, switch to KServe with a
FIPS-validated model serving runtime:

```bash
LLM_BACKEND_TYPE=kserve
KSERVE_ENDPOINT=https://granite.namespace.svc.cluster.local
KSERVE_MODEL_NAME=granite-3-8b-instruct
# KSERVE_TOKEN_FILE defaults to the in-pod SA token mount
KSERVE_VERIFY_SSL=true
```

Verify the InferenceService runtime (vLLM, TGIS) and the served model
are individually FIPS-aware — that's a model-by-model attestation
question, not something VirtValidate can answer.

---

## Validation

### Confirm host FIPS posture
```bash
cat /proc/sys/crypto/fips_enabled   # → 1
update-crypto-policies --show       # → FIPS (RHEL)
openssl list -providers              # FIPS provider must be active
```

### Confirm application FIPS posture
```bash
curl -s http://localhost:8000/api/system/fips-status | jq
```

Expected when both layers are aligned:
```json
{
  "configured": true,
  "detected": true,
  "effective": true,
  "mismatch_warning": null,
  "operations": [...]
}
```

If `mismatch_warning` is non-null, the message tells you exactly which
side is misconfigured.

### Verify TLS configuration
```bash
sslscan db.virtvalidate.local:5432
testssl.sh https://kserve.namespace.svc.cluster.local
```

Both must report TLS 1.2+ only and FIPS-approved cipher suites.

---

## Limitations

- **Ollama is not FIPS-validated.** Use KServe with an attested model serving runtime for full compliance.
- **Local model files** must come from trusted, signed sources.
- **Some Python dependencies** may not be FIPS-aware (e.g., libraries that ship their own crypto). The audit table below tracks what we use.
- **The application itself is not on the NIST CMVP list** — we operate as a *consumer* of FIPS-validated modules, not a validated module ourselves. This is the standard pattern for federal Python applications: rely on OpenSSL's validation.

---

## Approved configurations

| Component | FIPS Status | Notes |
|-----------|-------------|-------|
| OpenSSL (host) | Validated when host OS is in FIPS mode | The authoritative compliance boundary. |
| Python `cryptography` | Compliant transitively | Uses the host's OpenSSL via FFI. |
| Python `secrets` module | Compliant | Uses `os.urandom` → kernel CSPRNG. |
| Paramiko (SSH) | Compliant with RSA-3072 / ECDSA P-384 keys | The FIPS gate enforces this when `FIPS_MODE=true`. |
| PostgreSQL TLS | Compliant when configured per §5 above | Operator-configured. |
| KServe inference (TLS) | Compliant when `KSERVE_VERIFY_SSL=true` | Default. |
| KServe model serving runtime | Per model | Verify vLLM/TGIS + model individually. |
| Hash algorithms in app code | SHA-256 only | Audited; no MD5/SHA-1 anywhere. |
| Random number generation | `os.urandom` / kernel CSPRNG | No `random` module use in security-sensitive paths. |
| Ed25519 SSH keys | **Rejected when `FIPS_MODE=true`** | Not yet approved under FIPS 186-5. |

---

## What VirtValidate does NOT do

- Configure host OS FIPS mode (operator runs `fips-mode-setup`).
- Encrypt PostgreSQL data files at rest (operator provisions LUKS / CSI).
- Generate FIPS-compliant SSH keys (operator runs `ssh-keygen`).
- Issue or rotate TLS certificates (operator's PKI).
- Apply NetworkPolicies for stricter egress (operator's `kustomize` overlay or ingress controller).

These are intentional boundaries — federal compliance is a layered
attestation, not a single application setting. VirtValidate's
contribution is enforcing application-level choices and exposing the
posture so reviewers can audit the deployment in one round-trip.
