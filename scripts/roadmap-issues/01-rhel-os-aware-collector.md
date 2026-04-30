Current SSH collector targets RHEL 9. Federal customers run a mix of
RHEL 7, 8, 9, and (eventually) 10. Need to detect OS at start of
collection and branch command selection by version.

## Specific differences to handle

- `systemctl` output format changes between RHEL 7 and 8+
- `iptables` vs `nftables` availability
- Python 2 vs 3 defaults
- `chronyd` vs `ntpd` timing services
- DNF vs YUM package managers
- `ss` command syntax changes

## Acceptance criteria

- [ ] `app/core/ssh.py` reads `/etc/os-release` first and branches
  remaining commands by `ID` + major `VERSION_ID`.
- [ ] Per-OS command tables live in a single dispatch dict so adding a
  new OS doesn't fork the collection logic.
- [ ] Pytest covers the dispatch logic with synthetic os-release
  responses for RHEL 7 / 8 / 9 / 10.
- [ ] OS compatibility matrix added to `docs/COMPATIBILITY.md`
  (see companion issue).
