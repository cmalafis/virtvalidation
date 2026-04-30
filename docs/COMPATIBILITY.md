# Operating System Compatibility

VirtValidate's SSH collector detects the target VM's OS at the start of every
collection (see `app/core/os_profile.py`) and dispatches a per-OS
`CommandSet` (see `app/core/commands.py`) so RHEL 7 vs RHEL 9 vs Ubuntu
differences are confined to one place.

## Status definitions

| Status | What it means |
|---|---|
| **Tested** | Primary support target. CI runs against this OS where the GitHub-hosted runner image lets us. Bug reports for these are P0/P1. |
| **Best effort** | Likely works because the OS family is covered by an existing `CommandSet` branch, but we don't routinely run against this version. Bug reports accepted; fixes prioritized after Tested rows. |
| **Future** | Wired up in `CommandSet` but not yet supported in production. The collector will run against these without crashing, but verdicts haven't been validated against real data yet. |
| **Planned** | Not yet wired up. Tracked on the roadmap; ETA is the listed milestone. |

## Linux

| Distro | Status | Notes |
|---|---|---|
| **RHEL 7**       | Best effort | EOL but federal still runs it. Uses legacy iproute2 (`ss` lacks `-H`); collector strips header manually. iptables, yum, ntpd. |
| **RHEL 8**       | Tested      | Primary support target. Modern dispatch (nftables, dnf, chronyd). |
| **RHEL 9**       | Tested      | Primary support target. Same dispatch as RHEL 8. |
| **RHEL 10**      | Best effort | New release. Routes through the modern RHEL dispatch; collector smoke-tested against the dev preview. |
| **Rocky 8 / 9**  | Best effort | RHEL-derivative. `ID="rocky"` recognized; routes through modern RHEL dispatch. |
| **Alma 8 / 9**   | Best effort | RHEL-derivative. `ID="almalinux"` recognized; routes through modern RHEL dispatch. |
| **CentOS 7 / 8** | Best effort | EOL but still in-use. CentOS 7 routes through the legacy dispatch; CentOS 8 / Stream routes through the modern dispatch. |
| **Fedora 39+**   | Best effort | RHEL family. Routes through modern RHEL dispatch; bleeding-edge package versions occasionally surface drift. |
| **Ubuntu 22.04** | Future      | `CommandSet` exists (`apt`, `ufw`, `nftables`). Not yet exercised in CI. |
| **Ubuntu 24.04** | Future      | Same dispatch as 22.04. |
| **Debian 12**    | Future      | `ID="debian"` recognized; same dispatch as Ubuntu. |
| **Other Linux**  | Best effort | Unknown distros fall back to "modern Linux" defaults (systemd, `ip`, `ss -H`, `dnf`-or-`apt` autodetect). The snapshot is tagged `detection_confidence: "low"` so reviewers know they're in best-effort territory. |

## Windows

| Distro | Status | Notes |
|---|---|---|
| **Windows Server 2019** | Planned | v1.0.0 milestone. Will use a parallel `WinRMCollector`. |
| **Windows Server 2022** | Planned | v1.0.0 milestone. |
| **Windows Server 2025** | Planned | v1.0.0 milestone. |

Windows support is tracked in the [issue queue](https://github.com/cmalafis/virtvalidation/issues)
under the `federal` + `roadmap` labels.

## Detection confidence

Every snapshot's `meta.os_profile` block carries a `detection_confidence`
field with one of three values:

| Value | When it's emitted | Operational meaning |
|---|---|---|
| `high`   | Distro recognized **and** `VERSION_ID` parsed cleanly | Treat verdicts as authoritative for that OS family. |
| `medium` | Distro recognized but version unparseable / missing | Verdicts probably correct but assume the LLM doesn't know exact version-specific quirks. |
| `low`    | `/etc/os-release` empty, garbled, or distro unrecognized | Collector ran with modern-Linux defaults. Spot-check verdicts before acting on them. |

The dashboard's VM detail panel surfaces this badge so operators can
see at a glance when a VM landed in best-effort territory.

## Adding a new distro

1. Add a representative `os-release` payload to `tests/test_os_profile.py`
   (see the existing `OS_RELEASE_*` constants).
2. If the distro maps cleanly onto `rhel-like` or `debian-like`, no
   `commands.py` change is needed — the `_DISTRO_BY_ID` table in
   `os_profile.py` is the only update.
3. If the distro needs different commands (e.g., Alpine + busybox without
   systemd), add a new factory function to `commands.py` and extend the
   `command_set_for()` dispatch.
4. Add a row to the matrix above.
5. Regenerate the architecture diagram:
   `python3 scripts/generate_architecture_docs.py`
