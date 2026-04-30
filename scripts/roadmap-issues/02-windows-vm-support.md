Federal customers run significant Windows Server workloads alongside
Linux. VirtValidate currently only supports Linux via SSH. Add Windows
support via WinRM.

## Scope

- New `WinRMCollector` class parallel to `SSHCollector`.
- `pywinrm` library integration.
- Windows-specific baseline schema (services, scheduled tasks, registry,
  AD membership, Windows Updates, GPOs).
- Authentication: Kerberos (AD-joined) and NTLM (workgroup).
- Update LLM prompts to reason over Windows-specific data.
- Update VM enrollment UI to select connection protocol (SSH vs WinRM).
- Document AD service account setup requirements.

## Targets

- Windows Server 2019
- Windows Server 2022
- Windows Server 2025

**Stretch:** Windows Server 2016 (extended support until 2027).

## Out of scope

- WMI as a transport (WinRM is the strategic choice).
- Domain-controller-specific replication / FSMO collection (separate issue).
