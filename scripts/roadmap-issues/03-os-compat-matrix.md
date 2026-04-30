Build and maintain a tested compatibility matrix.

| OS              | Status      | Notes                          |
|-----------------|-------------|--------------------------------|
| RHEL 7          | Best effort | EOL but federal still runs it  |
| RHEL 8          | Tested      | Primary support target         |
| RHEL 9          | Tested      | Primary support target         |
| RHEL 10         | Best effort | New, partial testing           |
| Rocky 8/9       | Best effort | Likely works, RHEL-derived     |
| Alma 8/9        | Best effort | Likely works, RHEL-derived     |
| Windows 2019    | Planned     | v1.0.0 target                  |
| Windows 2022    | Planned     | v1.0.0 target                  |
| Windows 2025    | Planned     | v1.0.0 target                  |

## Acceptance criteria

- [ ] `docs/COMPATIBILITY.md` published with the matrix above.
- [ ] Each row defines what "Tested" / "Best effort" / "Planned" mean
  in terms of what we actually run in CI.
- [ ] CI runs the SSH collector smoke test against at least one
  containerized RHEL-derivative per primary target where the GitHub
  free tier permits (UBI images for RHEL 8/9 are reachable).
- [ ] Matrix is updated alongside any change to per-OS dispatch in
  `app/core/ssh.py` (companion issue).
