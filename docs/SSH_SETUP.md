# SSH key setup & target-VM hardening

VirtValidate validates VMs by SSHing into them and collecting structured
state. This document covers the security model, key distribution, and
the minimum sudoers rules each target VM needs.

---

## Security model in one paragraph

The appliance holds a **single Ed25519 private key**. The matching public
key is installed on every enrolled VM under one dedicated unprivileged
account. The collector reads system state with read-only commands —
`systemctl list-units`, `ip addr`, `ss -tulnH`, `findmnt`, etc. — and
returns the parsed output to the appliance. **Nothing is written on the
target VM.** The private key never leaves the appliance volume; the
**Settings → SSH Public Key** page is the only thing that exposes the
public half.

The threat model the design protects against:

- A compromised target VM cannot use the SSH session to reach the
  appliance (the appliance initiates).
- A compromised target VM cannot impersonate other VMs (each migrated
  pair shares one collector identity, but verdicts are tied to the
  source FQDN/IP recorded at enrollment).
- The appliance never accepts inbound SSH from anywhere — only outbound.

---

## 1. Generate the keypair on the appliance

Done once during installation (see [INSTALLATION.md](INSTALLATION.md#3)).
Reproduced here for reference:

```bash
mkdir -p backend/app/keys
ssh-keygen -t ed25519 -N "" \
    -f backend/app/keys/id_ed25519 \
    -C "virtvalidate@appliance"
chmod 600 backend/app/keys/id_ed25519
chmod 644 backend/app/keys/id_ed25519.pub
```

Why Ed25519 only:

- Smaller (~68 bytes) and faster than equivalent-strength RSA.
- Uniformly supported by OpenSSH 6.5+ — every RHEL/Ubuntu/SLES VM you
  care about migrating already speaks it.
- Shorter authorized_keys line means less paste friction during bulk
  enrollment.

---

## 2. View the public key

After the appliance is running, copy the public key from
**Settings → SSH Public Key** in the UI (the *Copy* button puts it on
your clipboard verbatim). Or pull it from the API:

```bash
curl -s http://localhost:8000/api/system/ssh-public-key | jq -r .public_key
```

The response includes a SHA-256 fingerprint you can compare against
`ssh-keygen -lf` output to verify integrity.

---

## 3. Install on each target VM

### Choose a username

VirtValidate connects as `virtvalidate` by default. You can override per
VM via the `ssh_user` field on enrollment (manual form, CSV, or RVTools
import). For consistency, create the same account on every VM:

```bash
# Run on each target VM as root
useradd -m -s /bin/bash virtvalidate
mkdir -p /home/virtvalidate/.ssh
chmod 700 /home/virtvalidate/.ssh
echo 'ssh-ed25519 AAAA...your-key... virtvalidate@appliance' \
    >> /home/virtvalidate/.ssh/authorized_keys
chmod 600 /home/virtvalidate/.ssh/authorized_keys
chown -R virtvalidate:virtvalidate /home/virtvalidate/.ssh
```

### Lock the account down

Prevent password logins, force key-only auth, and pin the allowed source
to the appliance's address:

```bash
# /etc/ssh/sshd_config.d/virtvalidate.conf
Match User virtvalidate
    PasswordAuthentication no
    KbdInteractiveAuthentication no
    AuthenticationMethods publickey
    AllowUsers virtvalidate@10.0.0.42   # appliance IP
    PermitTTY no
    X11Forwarding no
    AllowAgentForwarding no
    AllowTcpForwarding no
```

```bash
systemctl reload sshd
```

For OpenSSH < 7.4 (RHEL 7), drop the `Match` block and use the global
`AllowUsers` directive instead.

### Disable shell access for `virtvalidate`

The collector only ever runs commands listed in
[Section 5](#5-commands-the-collector-runs-on-each-vm). It never needs an
interactive shell. Ban shell logins entirely:

```bash
chsh -s /sbin/nologin virtvalidate
```

The non-interactive `ssh user@host '<command>'` invocations the collector
uses still work because the `ForceCommand` path in sshd doesn't go
through the user's login shell — but if you also want belt-and-suspenders
restriction, install a thin restricted shell that only allows the
documented commands.

---

## 4. Sudoers (only if you want full coverage)

Most of the collector's commands work as an unprivileged user. Three do not:

- `crontab -l -u <other_user>` — needs root for users other than the
  caller.
- Reading mode-`0600` cron files in `/etc/cron.d` — needs root.
- Reading services started under other users' systemd user instances — usually fine, but distro-specific.

If you want **complete** baseline coverage, grant the SSH user passwordless
read-only sudo on those three commands:

```bash
# /etc/sudoers.d/virtvalidate
Cmnd_Alias VV_CRONTAB = /usr/bin/crontab -l -u *
Cmnd_Alias VV_CRONFILES = /usr/bin/cat /etc/cron.d/*, /usr/bin/find /etc/cron.d *

virtvalidate ALL=(root) NOPASSWD: VV_CRONTAB, VV_CRONFILES
Defaults:virtvalidate !requiretty
Defaults:virtvalidate env_keep=
```

Validate with `visudo -c`. Without these, the collector still works —
you just lose visibility into per-user crontabs and root-owned cron
files. Drift around those areas would still be reported on whatever the
SSH user *can* read.

> **No sudo is needed for the rest of the baseline** (services, network,
> ports, mounts). Don't grant broader sudo than this; the principle of
> least privilege is what makes the design auditable.

---

## 5. Commands the collector runs on each VM

For your security review:

| Command | Purpose | Requires root? |
|---------|---------|----------------|
| `hostname -f \|\| hostname` | identity | no |
| `cat /etc/os-release` | OS family/version | no |
| `uname -r` | kernel | no |
| `systemctl list-units --type=service --state=running --no-legend --no-pager --plain` | running services | no |
| `ip -o -4 addr show` | IPv4 interfaces | no |
| `ip -o -6 addr show` | IPv6 interfaces | no |
| `ip -4 route show` | routing table | no |
| `cat /etc/resolv.conf` | DNS servers | no |
| `ss -tulnH` | listening ports | no |
| `findmnt -rn -o TARGET,SOURCE,FSTYPE,OPTIONS,SIZE,USED` | mounts | no |
| `cut -d: -f1 /etc/passwd` | enumerate users for crontab walk | no |
| `crontab -l -u <user>` | per-user crontab | only for users != self |
| `find /etc/cron.d /etc/crontab ... -maxdepth 1 -type f` | system cron file list | no (unless dir is `0700`) |
| `cat <path>` for each cron file found | system cron contents | depends on file mode |

The list is verifiable by reading `backend/app/core/ssh.py` — search for
`self._run(client, ...)`. There are no hidden commands.

---

## 6. Rotation

Replace the appliance keypair annually or after any suspected compromise:

```bash
# 1. Generate a new keypair (don't overwrite the old one yet)
ssh-keygen -t ed25519 -N "" \
    -f backend/app/keys/id_ed25519.new \
    -C "virtvalidate@appliance"

# 2. Distribute the new public key to every VM (alongside the old one)
for vm in $(podman exec backend curl -s localhost:8000/vms | jq -r '.[].source_hostname'); do
    ssh-copy-id -i backend/app/keys/id_ed25519.new.pub virtvalidate@$vm
done

# 3. Promote it
mv backend/app/keys/id_ed25519     backend/app/keys/id_ed25519.old
mv backend/app/keys/id_ed25519.new backend/app/keys/id_ed25519
mv backend/app/keys/id_ed25519.new.pub backend/app/keys/id_ed25519.pub
podman-compose restart backend

# 4. After confirming a successful baseline run with the new key,
#    remove the old public key from authorized_keys on each VM and
#    delete id_ed25519.old.
```

The audit log records every `baseline.collected` event, so you can
confirm a clean rotation by tailing the **audit log** tab and watching
for fresh entries against every VM.
