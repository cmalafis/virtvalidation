# Migration Planning Guide

VirtValidate's strategy-driven planner takes your migration intent
(captured through a wizard) and produces a wave-by-wave plan with
rationale for every decision. This guide covers how to choose a
strategy, common patterns federal customers use, how to write
effective freeform constraints, how to interpret rationale, and
when to revise a plan vs regenerate from scratch.

The planner is **decision support, not authoritative validation.**
Every plan needs an architect review before any wave runs.

---

## How the wizard works

The wizard captures eight pieces of customer intent:

| Step | Choice | What the LLM does with it |
|------|--------|---------------------------|
| 1 | Plan name | Carries through audit logs + the report PDF |
| 2 | Scope (which VMs) | Filters the inventory before any prompt is built |
| 3 | Primary grouping | Drives wave membership (application / vCenter folder / owner / environment / business unit / data classification / AI-decides) |
| 4 | Wave size target | Constrains each wave to roughly N VMs |
| 5 | Risk approach | Orders waves by risk (low first / high first / mixed) |
| 6 | Production handling | Sequences prod vs non-prod (mixed / non-prod first / dedicated prod waves) |
| 7 | Application atomicity | Whether one app can span multiple waves |
| 8 | Freeform constraints | Plain-English rules the LLM interprets |

The wizard's choices ride into the LLM prompt as explicit
instructions. The LLM **must** honor every structured choice — if it
can't satisfy a combination cleanly, it produces the closest plan
plus a plan-level warning explaining the gap.

---

## Choosing a primary grouping

This is the most consequential choice. The grouping determines
which VMs land in the same wave, which is what your operations
team has to coordinate.

### By application
**Pick when:** applications have clear boundaries and shouldn't be
split across waves. The cleanest mental model — each wave is "we're
migrating these N applications."

**Avoid when:** application boundaries are fuzzy or your VMs aren't
tagged with `application_hint`. The LLM falls back to naming
patterns; results suffer.

### By vCenter folder
**Pick when:** your folder structure already reflects how you want
to migrate (one folder per business unit, one per environment).

**Avoid when:** folders are accidental — VMs were dropped wherever.
The LLM doesn't know your folder taxonomy is meaningful unless your
operators say so.

### By application owner
**Pick when:** coordination across teams is more expensive than
technical efficiency. Each wave touches a small number of teams,
which makes change windows easier.

**Avoid when:** owner data is incomplete. VMs without an `owner`
field land in catchall waves.

### By environment
**Pick when:** you want classic dev → staging → prod sequencing.
Conservative, low-risk, well-understood.

**Avoid when:** you don't have clean environment tagging. The LLM
treats anything tagged `prod` or unclear as production; if half
your inventory is mistagged, the plan is wrong.

### By business unit
**Pick when:** matrix organization, BUs own their cutover windows
end-to-end. Each wave is one BU's responsibility.

**Avoid when:** you don't track BU on VMs. The wizard exposes
`owner` as a fallback; pick that instead.

### By data classification (federal)
**Pick when:** required by compliance — multi-classification
environments must NEVER mix levels in a single wave. UNCLASSIFIED,
CUI, SECRET migrate in separate waves.

**Avoid when:** you're not federal. This is overhead without value
in commercial contexts.

### Let AI decide
**Pick when:** you genuinely don't know the right grouping and want
a recommendation. The LLM analyzes the inventory and proposes one,
explaining its reasoning in `plan_summary`.

**Avoid when:** you have strong opinions. The LLM will surprise
you, and you'll spend more time arguing with the plan than if
you'd picked a strategy yourself.

---

## Common federal patterns

| Customer profile | Strategy |
|------------------|----------|
| Single hospital migrating clinical apps | By application + small waves + low-risk first + non-prod first + atomic apps |
| Multi-region cutover | By data classification + medium waves + mixed risk + non-prod first + atomic apps + freeform: "East before West" |
| Defense SECRET environment | By data classification + small waves + low-risk first + dedicated prod waves + atomic apps + freeform: "no migration windows during exercise X" |
| Civilian agency mixed cloud + on-prem fleet | By environment + medium waves + low-risk first + non-prod first + can-split apps |
| Research enclave with day-one NIST 800-53 audit | By application + small waves + low-risk first + dedicated prod + atomic apps + freeform: "every wave needs ATO sign-off, max 1 wave/week" |

These are starting points — every customer ends up with a tweaked
strategy after the first plan review.

---

## Writing effective freeform constraints

The freeform field is where the LLM gets the context that doesn't
fit any structured choice. Treat it like a brief to a smart
contractor — clear, specific, in plain English.

### Good examples

**Time-windowed restrictions:**
```
No migrations during March (annual financial close).
No migrations on Fridays after 2pm.
Hospital A maintenance window: Saturdays 22:00–04:00 only.
```

**Capacity / pace:**
```
Sarah's team can only handle one wave per week.
Maximum 3 waves per month due to change board cadence.
Database tier needs 2-week soak before app tier migrates.
```

**Application-specific rules:**
```
Epic EMR and Cerner imaging must NOT migrate in the same wave.
Oracle RAC nodes must all migrate together.
DNS must migrate first — every other wave depends on it.
ActiveDirectory domain controllers should be the last wave.
```

**Operational hints:**
```
Wave 1 should be a small "pilot" — pick 5 low-risk VMs to prove the process.
Reserve Q4 for application owners on PTO.
```

### Bad examples

```
Migrate efficiently.                     ← Too vague, no actionable signal.
Avoid problems.                          ← Same.
Use best practices.                      ← Same.
Migrate the application VMs first.       ← Use the structured "primary grouping" instead.
Small waves please.                      ← Use the structured "wave sizing" instead.
```

If a constraint maps to a structured choice, use the structured
choice. Save freeform for the things the wizard doesn't ask.

### Constraint conflicts

The LLM tries to honor every constraint. When two collide, it
produces the closest plan and flags the conflict via the plan-level
`warnings` array. Example:

> "You set wave size target to 10 VMs and application atomicity to
> 'all together', but application 'epic-emr' has 23 indivisible VMs.
> Wave 4 contains 23 VMs to keep epic-emr atomic; review whether
> the size cap can flex for this app or whether you want to switch
> atomicity to 'can_split'."

This is the consultative output — the plan tells you what tradeoff
it made and asks you to confirm.

---

## Interpreting rationale

Every wave includes a `rationale` field explaining:

- Why these VMs are grouped together
- Why this wave comes before/after others
- What customer-stated criteria drove the choice
- Any tradeoffs the LLM made

When you read rationale that doesn't match what you'd expect, that
usually means **either** your strategy didn't capture your intent
**or** the inventory data is missing a signal the LLM needed. Both
are fixable.

### Reading well

- Specific over generic. "These three VMs share the
  prod-array-tier1 datastore" is good. "These VMs are related" is
  not — push back.
- Cites your inputs. Good rationale references the application
  hint, environment field, or freeform constraint that drove the
  choice. Generic rationale that could apply to any plan is a
  smell.
- Honest about tradeoffs. Watch for `applications_split_warning`
  on waves where atomicity was relaxed.

### Reading poorly

- "Standard low-risk wave." → Not specific to your inventory.
  Probably means the LLM had no signal to differentiate. Add
  `application_hint` or owner data and regenerate.
- "Applications grouped by similarity." → No specific signal cited.
  Same fix.
- Identical rationale across multiple waves. → The LLM hit a
  pattern it can't break down further; revise the strategy or
  switch primary grouping.

---

## Revise vs regenerate

After reviewing a plan, you have two options:

### Revise (per-wave move)
**Pick when:** the strategy is right but specific VM-to-wave
assignments need adjusting. Move the VM, get a new revision number.
The revision chain preserves history — you can always see what
changed.

**Endpoint:** `POST /api/plans/{plan_id}/waves/{wave_number}/move-vm`

**UI:** "Move" button on each VM row in the plan view.

**Audit:** every move writes a `plan.revision.move_vm` audit row
with from-wave / to-wave / vm_id / actor / note.

### Regenerate
**Pick when:** the strategy itself is wrong. New plan, new strategy,
new rationale top-to-bottom.

**How:** /plans/new wizard. The previous plan stays in history;
nothing is lost.

### When in doubt

If you're moving more than ~10% of VMs across waves, you probably
want to regenerate with a different strategy. A revision is for
local adjustments; sweeping changes are a sign the strategy itself
is off.

---

## Async generation — what to expect

The wizard kicks off plan generation as a BackgroundTask. The
response is 202 Accepted with a `task_id`; the wizard polls
`GET /api/plans/generate/{task_id}/status` every 3 seconds.

Steps the operator sees:

1. **Queued** — task accepted, waiting for the BackgroundTask thread.
2. **Aggregating data** — inventory + baselines fetched.
3. **AI reasoning over your strategy** — the LLM call. This is the
   long step.
4. **Parsing AI response** — quick.
5. **Validating plan integrity** — checking every VM is in exactly
   one wave, every wave has rationale, no invented IDs.
6. **Saving plan** — DB write.
7. **Done** — `plan_id` is in the status response; the wizard
   navigates to `/plans/{id}`.

### Typical durations

| VM count | Ollama llama3:8b | KServe + vLLM |
|----------|------------------|---------------|
| <100 | 30–90 seconds | 10–20 seconds |
| 100–500 | 2–5 minutes | 30–90 seconds |
| 500–1,000 | 5–10 minutes | 1–3 minutes |
| 1,000–5,000 | 15–30 minutes | 3–10 minutes |
| >5,000 | hours / use hierarchical planner | depends |

For >5,000 VMs the wizard surfaces a warning: hierarchical
planning lands in v0.5+; until then large fleets work better split
across multiple plans by vCenter or environment.

### Failure modes

| Failure | Likely cause | Fix |
|---------|--------------|-----|
| `LLM backend failure` | Ollama / KServe unreachable | Check `/api/health/llm` |
| `Plan output was not valid JSON` | LLM hallucinated outside the schema | Re-trigger generation; rare with `temperature=0.1` |
| `Wave N rationale must be non-empty` | LLM skipped rationale | Re-trigger; if it persists, the prompt needs work |
| `did not place vm_ids ...` | LLM dropped some VMs | Re-trigger; happens 1% of the time on Ollama |
| `unknown vm_ids` | LLM hallucinated a VM id | Re-trigger; rare |
| `Scope filter matched zero VMs` | Bad scope filter | Adjust the scope step in the wizard |

The validator rejects anything that would produce a broken plan.
Re-triggering is safe — every plan gets a fresh `revision_number=1`.

---

## Federal compliance notes

- Every plan is audited at creation (`plan.generation_triggered` +
  `plan.generated`) and at each revision (`plan.revision.move_vm`).
  The audit row carries the actor, scope, strategy, and resulting
  vm/wave counts.
- The full prompt and raw LLM response are persisted on the plan
  row (`generation_prompt`, `generation_response`). Federal
  reviewers can reproduce any plan generation if they need to.
- The plan's `strategy_id` references the strategy that drove its
  generation — strategies are independently auditable.
- For SECRET / multi-classification environments, use the **By data
  classification** primary grouping option. The LLM is instructed
  to never mix levels in a single wave.
- FIPS-mode deployments work without changes — the planner uses
  the same LLM backend abstraction, so an in-FIPS-host KServe
  deployment is FIPS-compliant.

---

## Deferred work (planned for follow-on releases)

These are designed but not built today. The schema supports them.

| Feature | Status | Notes |
|---------|--------|-------|
| **"Why is this VM here?"** per-VM LLM explanation | v0.4 | One-shot LLM call per VM ID; reuses generation prompt |
| **Split this wave** | v0.4 | LLM proposes a split based on a criterion |
| **Merge with adjacent wave** | v0.4 | LLM evaluates safety + emits new combined rationale |
| **Plan comparison** (revision A vs B side-by-side) | v0.4 | Schema is in place via `supersedes_plan_id` chain |
| **Hierarchical planning** for >5,000 VMs | v0.5+ | Levels 2/3 of the planning hierarchy; see [SCALE.md](SCALE.md) |
| **"Mark wave reviewed"** approval workflow | v0.5 | Status flag + audit row per wave |

In the meantime, the move-vm action covers most refinement needs.
Regenerate when the strategy itself is wrong.
