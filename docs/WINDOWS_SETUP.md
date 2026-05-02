# Configuring Windows Server for VirtValidate

VirtValidate enrolls Windows Server VMs using **OpenSSH** as the
transport (not WinRM) and **PowerShell** as the remote shell. The
collector talks to Windows VMs the same way it talks to Linux VMs —
SSH in, run a command, parse structured output. This document covers
the per-VM setup operators need to do once, before VirtValidate can
capture a baseline.

Supported targets:

- Windows Server 2019 (build 17763) — OpenSSH ships built-in (install via WindowsCapability).
- Windows Server 2022 (build 20348) — recommended.
- Windows Server 2025 (build 26100) — best-effort; works the same way.

> Windows Server 2016 is **not** supported by this guide — it ships
> without OpenSSH and the manual install path is fragile. Operators
> with a 2016 fleet should plan to upgrade or skip those VMs.

---

## 1. Install OpenSSH Server

Run as Administrator in PowerShell:

```powershell
# Install the OpenSSH Server capability.
Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0

# Start the daemon and configure auto-start.
Start-Service sshd
Set-Service -Name sshd -StartupType Automatic

# Allow SSH through the Windows Firewall (the WindowsCapability
# installer adds a rule on Server 2022+ but not 2019; running this
# unconditionally is idempotent).
if (-not (Get-NetFirewallRule -Name "OpenSSH-Server-In-TCP" -ErrorAction SilentlyContinue)) {
  New-NetFirewallRule -Name "OpenSSH-Server-In-TCP" -DisplayName "OpenSSH Server (sshd)" `
    -Direction Inbound -Protocol TCP -LocalPort 22 -Action Allow
}
```

Verify the service is running:

```powershell
Get-Service sshd
# Should report Status=Running.
```

---

## 2. Set PowerShell as the Default Shell

**Critical.** Without this, every VirtValidate command gets handed to
`cmd.exe`, which doesn't understand the PowerShell pipeline. The
symptom is empty output on every collection.

```powershell
New-ItemProperty -Path "HKLM:\SOFTWARE\OpenSSH" `
  -Name DefaultShell `
  -Value "C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe" `
  -PropertyType String -Force
```

Restart sshd to pick up the change:

```powershell
Restart-Service sshd
```

> **Server 2025:** PowerShell 7 is available out-of-the-box at
> `C:\Program Files\PowerShell\7\pwsh.exe`. Either path works for
> VirtValidate's commands; we use Windows PowerShell 5.1 by default
> because it's universal across 2019/2022/2025.

---

## 3. Create the VirtValidate Service Account

Windows lacks a granular `sudo`. The `virtvalidate` account needs
**Administrator** privileges for the collector to read services,
firewall state, AD membership, and installed hotfixes.

```powershell
$Password = ConvertTo-SecureString "TempPass123!" -AsPlainText -Force
New-LocalUser -Name "virtvalidate" -Password $Password `
  -PasswordNeverExpires -UserMayNotChangePassword `
  -Description "VirtValidate appliance — read-only collector"

Add-LocalGroupMember -Group "Administrators" -Member "virtvalidate"
```

> **Security note.** This is a privileged account. The password set
> above is a placeholder for the bootstrap; once SSH-key auth is in
> place (next section) you should disable password auth entirely.
>
> For federal deployments (FIPS, FedRAMP, DoD), prefer:
>
> - Group Managed Service Accounts (gMSA) for credential rotation.
> - PIM (Privileged Identity Management) for time-bound elevation.
> - Audit logging of every collection session via Windows Event Forwarding.
>
> Document the privilege model in your operational SOP — federal
> reviewers ask about it explicitly.

---

## 4. Distribute the SSH Public Key

Get VirtValidate's public key from the appliance UI
(**Settings → SSH Public Key**) or via the API:

```bash
curl -s http://<virtvalidate-host>:8000/api/system/ssh-public-key | jq -r .public_key
```

For FIPS deployments, use RSA-3072 or ECDSA P-384 keys (see
[FIPS_DEPLOYMENT.md](FIPS_DEPLOYMENT.md)).

Drop the public key into the Windows VM's `authorized_keys`. Local
account on a member server uses
`C:\Users\virtvalidate\.ssh\authorized_keys`; an Administrator account
uses the special `C:\ProgramData\ssh\administrators_authorized_keys`
(this is an OpenSSH-on-Windows quirk).

```powershell
# Member server — local user keys live in their profile directory.
$sshDir = "C:\Users\virtvalidate\.ssh"
New-Item -ItemType Directory -Path $sshDir -Force | Out-Null

# Lock the directory down so only virtvalidate (and SYSTEM) can read it.
$acl = Get-Acl $sshDir
$acl.SetAccessRuleProtection($true, $false)
$acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule(
  "virtvalidate", "FullControl",
  "ContainerInherit,ObjectInherit", "None", "Allow")))
$acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule(
  "SYSTEM", "FullControl",
  "ContainerInherit,ObjectInherit", "None", "Allow")))
Set-Acl $sshDir $acl

# Append (not overwrite — preserves any pre-existing entries).
Add-Content -Path "$sshDir\authorized_keys" `
  -Value "ssh-rsa AAAA... virtvalidate@appliance"
```

If the account is in the Administrators group (which it is, per §3 above),
OpenSSH on Windows requires the `administrators_authorized_keys` file
**instead of** the per-user one. Use this path:

```powershell
$adminKeys = "C:\ProgramData\ssh\administrators_authorized_keys"
Add-Content -Path $adminKeys -Value "ssh-rsa AAAA... virtvalidate@appliance"

# OpenSSH refuses to use this file unless its ACL is locked to
# Administrators + SYSTEM — sshd logs "bad permissions" and silently
# ignores the keys otherwise.
icacls $adminKeys /inheritance:r `
  /grant "Administrators:F" `
  /grant "SYSTEM:F"
```

---

## 5. Verify SSH Works

From the VirtValidate appliance (or any host with a configured SSH
client):

```bash
ssh -i /app/keys/id_ed25519 virtvalidate@<windows-host-ip> \
  "Get-Service sshd | ConvertTo-Json -Compress"
```

Expected output is one line of JSON like:

```json
{"Name":"sshd","RequiredServices":[],"CanPauseAndContinue":false,"CanShutdown":false,...}
```

If you get plain text or an empty response, the default shell isn't
PowerShell — go back to §2.

---

## 6. Enroll the VM in VirtValidate

From the dashboard:

1. **+ Add VM** → **Manual** tab.
2. Hostname: `your-windows-host` (or its FQDN).
3. IP Address: optional but speeds up first connection.
4. SSH Username: `virtvalidate`.
5. Current Platform: **vmware** (or wherever it lives now).
6. **OS Hint: Windows Server** — pre-populates the inventory before
   the first probe.
7. Save.

Trigger a baseline capture from the VM's detail page (**📡 Capture Now**)
or wait for the scheduled run. The first capture's audit trail will
include an `ssh.host_key_accepted` row recording the Windows host's
key fingerprint — the standard TOFU acceptance.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| SSH connects but every command returns empty | Default shell is still cmd.exe | Re-run §2 and `Restart-Service sshd` |
| `Permission denied (publickey)` | Public key in wrong file | Admins use `administrators_authorized_keys`, not `~/.ssh/authorized_keys` |
| `Permission denied (publickey)` even after correct file | ACL on the keys file is too permissive | Run the `icacls` line in §4 |
| Capture completes but services list is empty | virtvalidate not in Administrators group | Re-run `Add-LocalGroupMember` from §3 |
| JSON parse errors in collector logs | PowerShell ExecutionPolicy blocking | `Set-ExecutionPolicy RemoteSigned -Scope LocalMachine` |
| First few characters of output look garbled | UTF-16 BOM (Server 2019 default) | The collector strips this automatically; if you still see it, check that the operator's PowerShell `$OutputEncoding` profile didn't override |
| `Get-Service` works but `Get-CimInstance` returns access denied | WMI service not running | `Start-Service Winmgmt; Set-Service Winmgmt -StartupType Automatic` |
| OpenSSH service won't start | Port 22 already taken | `netstat -aon \| findstr :22` to find the conflict; either move the conflicting service or change `sshd_config`'s `Port` directive |

---

## What the collector reads

For reference, the commands VirtValidate runs against each Windows VM
on every capture:

| Field collected | PowerShell command |
|-----------------|--------------------|
| OS detection | `Get-CimInstance Win32_OperatingSystem` |
| Services | `Get-Service` (filtered to Running) |
| Listening ports | `Get-NetTCPConnection -State Listen` |
| Volumes (mounts) | `Get-Volume` |
| Scheduled tasks (cron) | `Get-ScheduledTask` (Ready state) |
| Network interfaces | `Get-NetIPAddress` (v4 + v6) |
| Routes | `Get-NetRoute -AddressFamily IPv4` |
| DNS | `Get-DnsClientServerAddress` |
| Hotfixes | `Get-HotFix` |
| AD membership | `Get-CimInstance Win32_ComputerSystem` |
| Hostname | `[System.Net.Dns]::GetHostEntry($env:COMPUTERNAME).HostName` |

All commands pipe through `ConvertTo-Json -Compress` so the collector
never relies on text-scraping. Any new commands the team adds should
do the same.
