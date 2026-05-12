# SSH Key Guide

VirtValidate uses an SSH keypair to connect to every VM it manages.
The private key is materialized inside the appliance pod and never
leaves it; the public key is distributed to managed VMs via
`authorized_keys`. This guide covers the lifecycle: generation,
distribution, rotation, recovery, and the FIPS-mode constraints.

---

## Why an SSH key (and not credentials)

| Concern              | Why it rules out passwords |
|----------------------|----------------------------|
| Audit trail          | SSH password auth doesn't bind to a specific principal at the appliance side |
| Federal compliance   | FIPS, FedRAMP, IL5 all require keyed auth for systems automation |
| Rotation             | Password rotation forces touching every managed VM at once; key rotation can stagger |
| Secrets exposure     | A leaked password lets an attacker into every VM; a leaked private key needs them to also reach the appliance pod |

The SSH key is the appliance's **identity** — every audit row that
records an SSH connection includes the key's fingerprint, so federal
reviewers can correlate "did VirtValidate touch this VM" against
authorized_keys entries.

---

## Lifecycle

### 1. Initial generation (self-service)

On a fresh deploy, the Settings page renders a "Generate SSH Key"
panel in place of the public-key viewer. Click it:

1. Pick an algorithm (default depends on `FIPS_MODE`).
2. Confirm in the dialog.
3. The pod writes `id_<algo>` + `id_<algo>.pub` to the keys volume.
4. The Settings panel flips to the viewer state and shows the
   public key + fingerprint.

No pod shell access required. The endpoint refuses if a key already
exists at the target path — rotate instead.

### 2. Distribution

Copy the public key onto every VM the appliance will validate:

```bash
# Manual
echo 'ssh-ed25519 AAAA... virtvalidate-appliance' \
    | ssh user@vm.example.com 'cat >> ~/.ssh/authorized_keys'
```

The Settings page renders pre-populated snippets for **Ansible**,
**Puppet**, and **Terraform**. Pick the tab that matches your
configuration management and copy the snippet.

### 3. Rotation

Rotation creates a new keypair and renames the old one with a
`.bak.<unix-ts>` suffix. Both files stick around so a botched
rotation can be rolled back manually.

When to rotate:
  - Quarterly or annually as a baseline hygiene practice.
  - When the key (or its containing PVC) might be compromised.
  - When migrating from a non-FIPS algorithm to a FIPS-approved one.
  - When changing the appliance's identity (e.g. after a major
    upgrade, to force every managed VM to re-attest).

Rotation procedure:

1. Click **Rotate Key** in the Settings page.
2. Confirm the warning dialog. The new key is generated; the old
   one stays on the keys volume as a backup.
3. Update `authorized_keys` on every managed VM with the new
   public key — until you do, captures + validations will fail
   on those VMs.
4. After confirming all VMs accept the new key, delete the
   `.bak.<ts>` files from `/app/keys/` to complete the rotation.

The audit log captures the old + new fingerprints in a single
`ssh.key_rotated` entry so federal reviewers can trace the
transition.

---

## Algorithm choices

| Algorithm | Bits / Curve | FIPS 186-5 | Pick when |
|-----------|--------------|-----------:|-----------|
| Ed25519   | 256 (Curve25519) | ❌ | Default for non-federal deployments. Fastest, smallest signatures. |
| RSA       | 3072 | ✅ | Federal default. Widest compatibility with managed VMs. |
| ECDSA     | P-384 (secp384r1) | ✅ | Federal sweet spot — smaller keys than RSA-3072, full paramiko support. |

The Settings UI hides the non-FIPS option behind a disabled-state
tooltip when `FIPS_MODE=true`. The backend rejects the request
again at the API layer — never trust the UI's input validation
alone for a security gate.

### What's not supported

  - **DSA** (`ssh-dss`) — broken; rejected unconditionally.
  - **Smaller RSA / ECDSA** — RSA <3072, ECDSA P-256 are rejected
    under FIPS 186-5 in our implementation; the API normalizes to
    the FIPS-minimum size.
  - **Passphrase-protected keys** — not supported. The pod has no
    operator at startup to enter a passphrase; the key material
    sits at-rest encrypted via the storage class (PVC encryption)
    rather than via an OpenSSH passphrase.

---

## FIPS considerations

When `FIPS_MODE=true`:

  - `validate_algorithm_for_fips` rejects Ed25519 requests at the
    API layer with a 400.
  - The Settings dropdown defaults to RSA and disables Ed25519.
  - Existing Ed25519 keys are still **readable** for the
    `ssh.key_viewed` audit trail — federal customers need to see
    the non-compliant key to know they need to rotate.
  - The capture path (`app/core/ssh.py`) calls
    `app.core.fips.validate_ssh_key` on every load, so even if an
    operator slips an Ed25519 key onto the volume manually, the
    first capture call will reject it.

For full FIPS deployment guidance see
[`docs/FIPS_DEPLOYMENT.md`](./FIPS_DEPLOYMENT.md).

---

## Backup + recovery

### What gets backed up automatically

  - Every rotation renames `id_<algo>` and `id_<algo>.pub` to
    `id_<algo>.bak.<unix-timestamp>` before generating the
    replacement. Backups stay on the same PVC.

### What you should back up out-of-band

  - The entire keys PVC. On Kubernetes, snapshot the
    `<release>-ssh-keys` PVC via your CSI's VolumeSnapshot resource
    on the same cadence you back up Postgres data.
  - The fingerprint of the active public key, recorded against the
    appliance's serial / hostname in your CMDB. If the keys volume
    is lost, you need to know which fingerprint your
    `authorized_keys` entries match.

### Recovering from a lost private key

If the keys volume is lost and a backup snapshot is available:
1. Restore the snapshot to a new PVC.
2. Re-attach to the appliance.
3. Verify the fingerprint matches what's in your CMDB +
   `authorized_keys`.

If no snapshot is available:
1. Use **Rotate Key** to generate a new keypair.
2. Push the new public key to every managed VM
   (`authorized_keys`).
3. The appliance has to wait for the rollout to complete before
   captures + validations resume on those VMs.

---

## Audit log entries

Every state transition writes a structured row to the audit log:

| Action               | Details                                            |
|----------------------|----------------------------------------------------|
| `ssh.key_generated`  | algorithm, fingerprint, path                       |
| `ssh.key_rotated`    | old_algorithm, old_fingerprint, new_algorithm, new_fingerprint, backups[] |
| `ssh.key_viewed`     | algorithm, fingerprint (only when the key exists)  |

Federal reviewers can reconstruct the full key lifecycle from these
three actions alone. The **missing**-state read does NOT emit an
audit row — Settings page polling on a freshly-deployed appliance
would otherwise spam the trail.

---

## API reference

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/system/ssh-key` | GET | Wrapped status response (exists / missing) |
| `/api/system/ssh-key/generate` | POST | One-time keypair generation; 409 on conflict |
| `/api/system/ssh-key/rotate` | POST | Backup-and-replace; 404 when no key to rotate |
| `/api/system/ssh-public-key` | GET | Back-compat shim; returns the old `{public_key, fingerprint, type}` shape. New callers should use `/ssh-key` instead. |

Request body for generate + rotate:

```json
{ "algorithm": "ed25519" }   // optional; default = SSH_KEY_ALGORITHM env var
```

Response shape for `GET /ssh-key`:

```json
// exists
{
  "status": "exists",
  "algorithm": "ed25519",
  "fingerprint": "SHA256:...",
  "public_key": "ssh-ed25519 AAAAC3... virtvalidate-appliance",
  "created_at": "2026-05-11T..."
}

// missing
{
  "status": "missing",
  "configured_algorithm": "ed25519",
  "expected_path": "/app/keys/id_ed25519"
}
```

---

## Related docs

- [`docs/INSTALLATION.md`](./INSTALLATION.md) — first-time setup,
  including the generate-key step.
- [`docs/SECURITY.md`](./SECURITY.md) — appliance security posture.
- [`docs/FIPS_DEPLOYMENT.md`](./FIPS_DEPLOYMENT.md) — FIPS-mode
  algorithm selection + enforcement chain.
- [`backend/app/core/ssh_key.py`](../backend/app/core/ssh_key.py) —
  generation + rotation source of truth.
- [`backend/tests/test_ssh_key_api.py`](../backend/tests/test_ssh_key_api.py) —
  pinned response shapes + FIPS gating semantics.
