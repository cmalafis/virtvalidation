Optional integration where VirtValidate commits all generated artifacts
to a customer-specified Git repository.

## Artifacts to commit

- MTV YAML (`Plan`, `NetworkMap`, `StorageMap`)
- Wave PDF reports
- Validation result JSON
- Ansible playbooks (once implemented — see future issue)

Each commit message includes wave number, timestamp, and a one-line
summary so a `git log` reads as a complete migration audit trail.

## Configuration

- Git repo URL
- SSH key or token for authentication
- Branch (default: `main`, configurable)
- Commit message template
- Path prefix within the repo (e.g. `migrations/<plan-id>/wave-<n>/`)

## Why

Customers get full audit history of every action VirtValidate took,
integrated with their existing Git-based workflows (review, rollback,
SOC 2 / compliance evidence). Federal customers in particular often
require artifact-level immutability that Git provides for free.

## Acceptance criteria

- [ ] New settings panel section: "Git artifact sync".
- [ ] On enable, every artifact-producing endpoint commits + pushes
  on success (best-effort with audit-log entry on failure — never
  blocks the user-facing operation).
- [ ] Backend uses libgit2 / pygit2 (no shelling out to `git`).
- [ ] Auth supports both SSH key (file path) and HTTPS token (env
  var or settings record, encrypted at rest).
- [ ] Disabled by default; opt-in only.
