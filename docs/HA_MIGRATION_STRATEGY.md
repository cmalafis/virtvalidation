# HA Migration Strategy

VirtValidate's planner defaults to **spreading HA cluster members
across migration waves** so the cluster's original-side service
stays available throughout the migration window. This document
covers what that means, when to opt out, and how detection works.

---

## TL;DR

- Default: `ha_strategy: "spread"` — each HA cluster's members go to
  separate waves. Primary first, replicas later.
- Opt-out: `ha_strategy: "together"` — migrate the whole cluster in
  one wave. Faster, but the cluster experiences full downtime during
  cutover.
- Hybrid: `ha_strategy: "auto"` — spread for clusters with ≥3
  members, together for pairs.

Pick **together** only when you have a planned maintenance window
and want to minimize total elapsed migration time. Pick **spread**
in every other case — federal customers default here.

---

## Why "spread" is the default

A migration cuts over one VM at a time: source-side workload
freezes, disk synchronizes, destination-side VM boots. During that
window the cut-over VM serves nothing.

If a 3-node Postgres cluster migrates together in one wave:
  - All three replicas freeze simultaneously.
  - The application sees DB unavailability for the full migration
    duration (often 10-30 minutes per VM, longer for large
    datastores).
  - There's no quorum on either side to serve writes.

If the same 3-node cluster migrates spread across 3 waves:
  - Wave 1 takes the primary down briefly — replicas promote and
    serve.
  - Wave 2 moves replica-01 — still 2 nodes serving (now on a mix
    of source + destination).
  - Wave 3 moves replica-02 — the migration window passes without
    quorum loss.

This is the standard "rolling cutover" pattern federal customers
use today; VirtValidate just defaults to it.

---

## When to use `together`

Only when **all** of these apply:
  - You have a planned maintenance window and downtime is
    acceptable.
  - The application doesn't have an HA quorum to lose (e.g. a
    development sandbox cluster that's idle overnight).
  - You'd rather complete migration in one wave than stretch it
    over multiple cutovers.
  - The cluster fits comfortably in a single migration window
    (sub-30-minutes total).

For federal production migrations this is rare — most teams prefer
shorter unavailability spread across more waves than a single long
window.

---

## How HA detection works

The preclassifier inspects every VM's name and tags it with one of:

| `ha_role`     | Trigger |
|---------------|---------|
| `primary`     | Name contains `primary`, `leader`, `master`, `active`, `writer`, `main` |
| `replica`     | Name contains `replica`, `secondary`, `follower`, `reader`, `slave` |
| `standby`     | Name contains `standby`, `passive`, `backup` |
| `member`      | Sequential numeric suffix (no pattern match) AND not the lowest-numbered |
| (primary)     | Sequential numeric suffix AND lowest-numbered |
| `standalone`  | None of the above |

When at least one VM in a group matches a primary/replica/standby
pattern, the pattern result wins. When no patterns match but the
group has sequentially-named members (`-01`, `-02`, `-03`), the
lowest-numbered VM becomes the "primary" and the rest become
"member" — appropriate for symmetric clusters (Cassandra, etcd)
where no node is more important than the others.

### Edge cases

  - **Single VM** — always `standalone`. No HA decision to make.
  - **Two non-sequential names** — `standalone` for both. The
    classifier won't guess HA from name alone without supporting
    evidence.
  - **Operator-supplied `role` override** — if the operator has
    tagged the VM with an explicit role, it wins over both name
    patterns and sequence inference.

### What's NOT detected

  - Cross-cluster HA (replica in vCenter B fails over to primary
    in vCenter A) — the preclassifier never merges across
    vCenters, so this case looks like two unrelated groups today.
    Tracked in roadmap.
  - Application-level HA without name/role signals (e.g. a JVM
    cluster discovered via JGroups). Falls back to `standalone`
    for now — operator can override post-generation via
    `POST /api/plans/{id}/waves/{n}/move-vm`.
  - Active/passive failover between dissimilarly-named VMs
    (`payroll-svc` and `payroll-failover`). Falls back to
    `standalone`.

---

## What "spread" produces

When `ha_strategy="spread"` runs against the reference test fleet's
EHRPro data tier (3 postgres VMs), the preclassifier first builds
one group (`vc1/ehrpro-prod/data/stateful/hint:ehrpro:data`) with
3 ha_members, then `split_ha_group` expands it into three
micro-groups:

| Group ID                                                | vm_ids | ha_role |
|---------------------------------------------------------|--------|---------|
| `.../hint:ehrpro:data/ha:primary:ehrpro-postgres-db-01` | [1]    | primary |
| `.../hint:ehrpro:data/ha:member:ehrpro-postgres-db-02`  | [2]    | member  |
| `.../hint:ehrpro:data/ha:member:ehrpro-postgres-db-03`  | [3]    | member  |

The wave assigner (LLM or mechanical) then places each micro-group
in a successive wave. Risk + state are preserved from the parent
group — the mitigations on a primary's wave still say "verify
backup completed".

---

## Configuration

API:

```http
POST /api/plans
Content-Type: application/json

{
  "vm_ids": [...],
  "ha_strategy": "spread"
}
```

| Value       | Behavior                                                  |
|-------------|-----------------------------------------------------------|
| `spread`    | Default. Every HA group splits into per-member micro-groups. |
| `together`  | Every HA group stays as one cohesive group.               |
| `auto`      | Spread for ≥3-member clusters; together for pairs.        |

UI (Plan Wizard) surfaces the choice as a two-option toggle with
the same defaults.

---

## Audit trail

Every plan response carries the `ha_strategy` it was generated
with in the audit log payload, along with the resulting
`groups_formed` count. Federal reviewers can trace why a given
migration was sequenced the way it was without rerunning the
planner.

---

## Related docs

- [`docs/PLANNING_ARCHITECTURE.md`](./PLANNING_ARCHITECTURE.md) —
  the broader two-stage planner design.
- [`docs/RISK_ASSESSMENT.md`](./RISK_ASSESSMENT.md) — how the
  per-group risk factors + mitigations get assembled.
- `backend/tests/test_ha_strategy.py` — pinned contracts for HA
  detection + spread + together + auto.
