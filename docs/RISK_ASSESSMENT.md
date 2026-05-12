# Risk Assessment

Every preclassifier group carries a `RiskAssessment` block — the
plan UI surfaces this so operators can see *why* a wave was rated
high-risk, and *what to do about it* before cutover. This document
catalogues the factors and mitigations the assessor produces, plus
the rollback-complexity and downtime estimates that round out the
display.

---

## Output shape

```json
{
  "level": "high",
  "factors": [
    "Stateful service with persistent data",
    "Database / data tier — connection loss affects dependent applications",
    "Sequential cluster of 3 members — coordinated cutover across multiple nodes"
  ],
  "mitigations": [
    "Verify backup completed within 4 hours of migration",
    "Coordinate with application team for connection draining before migration",
    "Spread HA members across waves (ha_strategy=spread is the default) so at least one node serves the original infrastructure throughout the migration window"
  ],
  "rollback_complexity": "high",
  "estimated_downtime": "10-30 minutes per VM (connection drain + cutover)"
}
```

The catalog below is **fixed** — the assessor never produces
free-text factors / mitigations. Federal audit trails can
correlate factors across plans by string match without keyword
drift.

---

## Factor catalog

| Factor                                                            | Triggers when                              |
|-------------------------------------------------------------------|--------------------------------------------|
| Stateful service with persistent data                             | `state == "stateful"`                      |
| Database / data tier — connection loss affects dependent applications | `role == "data"`                           |
| Infrastructure service — failure affects entire cluster           | `role == "infrastructure"`                 |
| No HA replication detected — single point of failure              | `state == "stateful"` AND < 2 HA peers     |
| Sequential cluster of N members — coordinated cutover             | Has sequential names AND vm_count ≥ 2      |
| Large group (N VMs) — migration time and rollback complexity scale | `vm_count > 10`                            |

Adding a new factor is a code change in
`app.core.preclassifier.assess_risk`. Federal customers will ask
us why a new factor showed up the first time it does, so the
review process is intentionally slow — every factor is a stable
audit primitive once it ships.

---

## Mitigation catalog

| Mitigation                                                                  | Pairs with factor              |
|-----------------------------------------------------------------------------|--------------------------------|
| Verify backup completed within 4 hours of migration                         | Stateful                       |
| Coordinate with application team for connection draining before migration   | Data tier                      |
| Ensure secondary AD / DNS / PKI available during migration                  | Infrastructure                 |
| Consider establishing HA replica before migration or schedule a documented maintenance window | No HA + stateful |
| Spread HA members across waves (ha_strategy=spread is the default) so at least one node serves the original infrastructure throughout the migration window | Sequential cluster |
| Run pilot migration on 2-3 representative VMs in a lower environment before the full wave | Large group |

Like factors, mitigations are a fixed catalog. The audit benefit
of consistent wording outweighs the editorial benefit of bespoke
prose.

---

## Risk-level derivation

The headline `level` is purely a function of factor count:

| Factor count | Level    |
|-------------:|----------|
| 0            | `low`    |
| 1 - 2        | `medium` |
| ≥ 3          | `high`   |

This is deliberately simple. Operators decide what to do with the
factors; the level is a sorting key, not a verdict. Stacking three
factors against one VM produces the same `high` level as five
factors against another — the underlying detail still shows in the
factors list.

---

## Rollback complexity

A separate dimension from forward-migration risk. Operators ask
"if this wave fails, how hard is it to undo?" — that's not the
same question as "how risky is the cutover":

| `rollback_complexity` | Triggers when                       |
|-----------------------|-------------------------------------|
| `high`                | `state == "stateful"`               |
| `medium`              | `role == "infrastructure"`          |
| `low`                 | Everything else                     |

Stateful rollback is hard because the data has already drifted by
the time you decide to revert. Infrastructure rollback is medium
because re-registering cluster services takes coordination.
Stateless rollback is low because nothing has state to lose.

---

## Estimated downtime

A coarse bucket — exact timing depends on datastore class, vm disk
size, and network throughput, which the planner doesn't know.

| State / role combo            | Downtime estimate                                |
|-------------------------------|--------------------------------------------------|
| `state == "stateless"`        | 0 (live migration possible)                      |
| `role == "data"` (stateful)   | 10-30 minutes per VM (connection drain + cutover) |
| `role == "infrastructure"`    | 5-15 minutes per VM                              |
| default                       | 5-15 minutes per VM                              |

These match what MTV / Forklift reports in practice for the
appliance's reference deployment. Customers running on faster
storage (NVMe + 100GbE) routinely beat these; on commodity NFS
they trail. The estimate is a planning anchor, not a SLA.

---

## How aggregation works at the wave level

When a wave contains multiple groups, the plan UI shows the wave's
aggregate risk as the **MAX of group risks**. The UI also
deduplicates factors + mitigations across groups so the wave's
expanded panel doesn't repeat the same backup mitigation three
times.

The deduplication is a UI concern; the API returns each group's
own `risk_assessment` intact so consumers that want
per-group detail (audit log exporters, MTV YAML annotators) get it.

---

## Plan UI display

(Today the UI shows `risk_assessment` as a JSON blob on the plan
detail page; the expandable per-wave panel described in the spec
is open follow-on work — see `docs/PLAN_OVERRIDES.md` for the
related drag-and-drop UI deferral.)

The pinned contract on the response side is:

  - Each group dict in `result["groups"]` has a non-null
    `risk_assessment` with the four fields above.
  - The wave-level aggregate is the responsibility of the UI / CLI
    consumer; the API exposes per-group detail to enable any
    aggregation logic.

---

## Related docs

- [`docs/HA_MIGRATION_STRATEGY.md`](./HA_MIGRATION_STRATEGY.md) —
  ha_strategy:spread is itself listed as a mitigation factor when
  sequential clusters are detected.
- [`docs/PLANNING_ARCHITECTURE.md`](./PLANNING_ARCHITECTURE.md) —
  the two-stage planner that produces the groups + risks.
- `backend/tests/test_ha_strategy.py` — pinned contracts for risk
  level derivation + factor / mitigation generation.
