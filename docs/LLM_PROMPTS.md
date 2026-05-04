# LLM Prompt Engineering Notes

VirtValidate sends prompts to its LLM backend (Ollama / KServe /
vLLM via the `LLMBackend` abstraction) for four distinct tasks:

| Task | Module | Prompt purpose |
|------|--------|----------------|
| **Validation** | `app/core/llm/client.py` | Reason over baseline-vs-current VM diff, produce verdict + findings |
| **Wave planning** (legacy single-shot) | `app/core/planner.py` | Group VMs into dependency-ordered migration waves |
| **Strategy-driven planning** | `app/core/strategy_planner.py` | Wave plan honoring 8 wizard choices + freeform constraints |
| **Network design review** | `app/core/network_review.py` | Gap analysis between VMware networking and proposed CUDN/NAD/NetworkPolicy |
| **Storage design review** | `app/core/storage_review.py` | Gap analysis between VMware datastores and proposed StorageClass / VolumeSnapshotClass |
| **Level 1 categorization** | `app/core/categorizer.py` | Batched grouping of VMs into application/environment/business-unit clusters |

This doc focuses on the **strategy-driven planning** prompt. The
others follow the same structural conventions; differences are
domain-specific.

---

## Strategy planning prompt structure

The prompt has three layers:

1. **System prompt** — single static string in
   `app.core.strategy_planner.SYSTEM_PROMPT`. Defines the LLM's
   role, output schema, and rules ("MUST honor every structured
   choice", "DO NOT silently violate a constraint").
2. **User message** — built per-call by `build_user_prompt`.
   Injects the customer's wizard choices, freeform constraints,
   and VM inventory.
3. **Output schema** — strict JSON. Validated by `_parse_and_validate`
   after the LLM returns; any rule violation raises
   `StrategyPlannerError` and the caller marks the task failed.

### System prompt design choices

The system prompt is deliberately:

- **Imperative.** "You MUST honor every structured strategy
  choice." Not "please consider" or "try to". LLMs interpret
  imperative instructions more reliably across model sizes.
- **Schema-prescriptive.** The exact JSON schema is in the system
  prompt with field types and required-keys notes. This pins the
  LLM to the format the parser expects — drift is a parse failure,
  not a silent shape mismatch.
- **Honest about consultative output.** "Your output is
  consultative — operators will read your rationale to decide
  whether to proceed, adjust, or push back." Sets the model up
  to write rationale for humans, not for downstream automation.
- **Failure-mode explicit.** "If you cannot satisfy a strategy +
  constraints combination cleanly, produce the closest plan you
  can AND document the gap in the plan-level warnings." Gives the
  LLM a graceful escape valve — without it, the LLM either
  silently violates a constraint or refuses to plan.

### User message structure

```
## Customer strategy choices

- Primary grouping: <imperative fragment>
- Wave size target: <approximate count>
- Risk approach: <imperative fragment>
- Production handling: <imperative fragment>
- Application atomicity: <imperative fragment>

## Customer freeform constraints (interpret pragmatically)

<verbatim text from wizard, or "(none provided)">

## VM inventory (N VMs)

```json
[ ... lightweight VM rows ... ]
```

Produce the JSON object specified in the system prompt now.
```

Key design choices:

- **Strategy fragments are dispatch-table-driven.** Every wizard
  choice maps to a fixed string in `_PRIMARY_GROUPING_FRAGMENTS`
  / `_RISK_FRAGMENTS` / etc. Adding a new choice is a one-line
  change, not a prompt rewrite. Each fragment is imperative and
  carries the *why* alongside the *what*: "GROUP BY ENVIRONMENT.
  Sequence dev → staging → prod. Migrate every dev VM before
  moving any staging VM..." gives the LLM both the rule and the
  expected behavior.
- **Freeform rides through verbatim.** No paraphrasing, no LLM
  pre-processing. The downstream LLM sees exactly what the
  operator typed. Tradeoff: garbage in → garbage in the prompt.
  Worth it because operators sometimes encode constraints in
  ways we'd lose by re-rendering.
- **VM inventory is JSON-formatted.** Easier for the LLM to
  reason about than prose. Keys are the same field names the LLM
  sees in `/api/vms`, so an LLM that's been used for validation
  already knows the shape.
- **Lightweight rows only.** We strip to {vm_id, name, role,
  environment, owner, application_hint, vsphere_networks,
  vsphere_datastores, target_*, baseline}. Sending the full VM
  record explodes the prompt token budget without helping
  reasoning.

### Output schema (verbatim from system prompt)

```json
{
  "plan_summary": "two-or-three-sentence plain-English overview",
  "rationale": "longer paragraph explaining how this plan honors the strategy",
  "warnings": ["plain-English warnings about tradeoffs"],
  "waves": [
    {
      "wave_number": 1,
      "name": "Wave 1: Foundational Infrastructure",
      "vm_ids": [<int>, ...],
      "rationale": "specific reason these VMs are in this wave",
      "estimated_duration": "human-readable estimate",
      "risk_level": "low" | "medium" | "high",
      "considerations": "anything operators should know",
      "applications_included": ["list", "of", "app", "names"],
      "applications_split_warning": "non-empty when split; null otherwise"
    }
  ],
  "next_actions": ["operator checklist items"]
}
```

The parser validates every required field. Specific rules:

- `waves` must be non-empty
- Every input `vm_id` appears in **exactly one** wave
- No invented `vm_id`s (parser rejects unknown IDs)
- `rationale` non-empty per wave (the consultative deliverable)
- `risk_level` ∈ {low, medium, high}
- `vm_ids` must all be ints

If any rule fails, the parser raises `StrategyPlannerError` and
the BackgroundTask marks the generation failed. The operator can
re-trigger; failures are non-billable from a workflow standpoint.

---

## Worked example

### Inputs

**Strategy:**
- Primary grouping: by_application
- Wave size: small_5_10
- Risk approach: low_first
- Production handling: non_prod_first
- Application atomicity: all_together
- Freeform: "DNS must migrate first. Database tier needs 2-week soak."

**VM inventory** (4 VMs):
```json
[
  {"vm_id": 1, "name": "dns-01", "role": "dns", "environment": "prod", "application_hint": "dns"},
  {"vm_id": 2, "name": "billing-app-01", "role": "app", "environment": "prod", "application_hint": "billing"},
  {"vm_id": 3, "name": "billing-db-01", "role": "db", "environment": "prod", "application_hint": "billing"},
  {"vm_id": 4, "name": "billing-app-02", "role": "app", "environment": "dev", "application_hint": "billing"}
]
```

### Expected LLM output

```json
{
  "plan_summary": "Three-wave migration: DNS first (the foundational dependency the customer flagged), then billing-dev to prove the process, then billing-prod after a 2-week soak. Honors atomic application grouping while respecting non-prod-first sequencing within the constraints of the customer's freeform rules.",
  "rationale": "The customer's freeform constraint 'DNS must migrate first' takes precedence over the standard non-prod-first sequencing — DNS is migrated as wave 1 even though it's a prod VM. The remaining VMs follow non-prod-first: billing-app-02 (dev) is wave 2, billing-prod (app + db together to honor atomicity) is wave 3. The 2-week soak is operationally enforced — wave 3 is annotated as needing the soak window before kickoff.",
  "warnings": [
    "DNS (wave 1) is a production VM placed before non-prod billing-app-02 (wave 2), violating the strict non-prod-first rule. The customer's freeform DNS-first constraint took precedence; if this is wrong, switch DNS to a non-prod VM or relax the freeform constraint."
  ],
  "waves": [
    {
      "wave_number": 1,
      "name": "Wave 1: DNS Foundation",
      "vm_ids": [1],
      "rationale": "Customer's freeform constraint requires DNS to migrate first. Single-VM wave because DNS is its own application and should not be combined with other prod-tier services until proven.",
      "estimated_duration": "1-2 hours",
      "risk_level": "low",
      "considerations": "Verify DNS resolution from a test VM before declaring wave complete.",
      "applications_included": ["dns"],
      "applications_split_warning": null
    },
    {
      "wave_number": 2,
      "name": "Wave 2: Billing Non-Prod",
      "vm_ids": [4],
      "rationale": "Single non-prod billing VM serves as a process-proof for the billing application before the prod wave runs. Honors non-prod-first within the post-DNS sequencing.",
      "estimated_duration": "2-3 hours",
      "risk_level": "low",
      "considerations": "Treat findings here as gating signal for wave 3.",
      "applications_included": ["billing"],
      "applications_split_warning": null
    },
    {
      "wave_number": 3,
      "name": "Wave 3: Billing Production",
      "vm_ids": [2, 3],
      "rationale": "Production billing tier (app + db) migrates as one wave to honor application atomicity. Gated by the customer's 2-week soak requirement after wave 2.",
      "estimated_duration": "4-6 hours",
      "risk_level": "high",
      "considerations": "Customer-mandated 2-week soak between wave 2 completion and wave 3 kickoff. Schedule the change window 14+ days after wave 2 sign-off.",
      "applications_included": ["billing"],
      "applications_split_warning": null
    }
  ],
  "next_actions": [
    "Schedule wave 1 DNS migration window",
    "Verify wave 2 (billing-dev) acceptance criteria with billing app owner",
    "Block wave 3 in the change calendar at wave-2-complete + 14 days"
  ]
}
```

What the LLM did well:
- Honored the freeform DNS-first constraint
- Honored atomic application grouping (billing app + db together in wave 3)
- Surfaced the prod-first conflict via plan-level warning rather than hiding it
- Per-wave rationale cites specific customer signals
- Operationalized the 2-week soak in `considerations`

---

## Tuning for different model sizes

Tested against:

| Model | Behavior |
|-------|----------|
| **llama3:8b** | Reliable for ≤100 VMs. Above that, occasional rationale repetition or skipped warnings. Recommended for standalone deployments. |
| **llama3:70b** | Better rationale quality, handles ≤500 VMs cleanly. Worth the GPU if available. |
| **granite-3-8b-instruct** | RHOAI default. Comparable to llama3:8b for our prompt; slightly better at honoring strict constraints. |
| **mistral:latest** | Faster but more drift on output schema. Workable but expect more parser rejections. |

### Tuning knobs

`temperature=0.1` is the default — low enough for stable JSON
output, high enough to allow some variation in rationale
phrasing. Raising it produces more creative rationale but more
schema drift.

`max_tokens` is unset by default. The strategy planner doesn't
cap output length; for inventories >1,000 VMs the response can
exceed 32k tokens. KServe deployments should set
`KSERVE_TIMEOUT_SECONDS=300` or higher to accommodate large
inventories.

---

## Known limitations

### Output drift on edge cases

The LLM occasionally:

- **Drops a VM** from the waves list. Parser catches this and
  rejects with `did not place vm_ids ...`. Re-trigger.
- **Hallucinates a VM ID** not in the input. Parser catches and
  rejects with `unknown vm_ids`. Re-trigger.
- **Skips rationale on one wave.** Parser rejects with
  `rationale must be non-empty`. Re-trigger.

These happen ~1% of the time on llama3:8b and effectively never
on granite-3-8b-instruct or larger models. The parser's
strict-rejection stance means they surface as failed
generations, not subtle bugs in the plan.

### Constraint conflicts

When two constraints conflict, the LLM picks the closest plan
and writes a warning. The choice it makes is not always the
choice the operator would make. Read the warnings before
accepting any plan with non-empty `warnings`.

### Long inventories

Above ~1,000 VMs, rationale quality degrades — the LLM starts
producing template-y rationale that doesn't differentiate waves.
Hierarchical planning (Level 2 + 3, deferred to v0.5+) is the
real fix. Until then, split large fleets across multiple
strategy-driven plans by vCenter or environment.

### Federal compliance gaps

The LLM output is captured verbatim in `generation_response`
on the plan row. Federal reviewers can audit the chain. **But:**
the LLM's reasoning is not "explainable" in the formal sense
(no chain-of-thought capture). Treat plans as decision support
needing architect sign-off — never as an authoritative source.

---

## Validation prompt notes (briefer)

The validation prompt (`app/core/llm/client.py`) follows the
same conventions:

- Imperative system prompt ("You MUST")
- Severity tier definitions baked in (CRITICAL / HIGH / MEDIUM / LOW)
- Explicit ignore list (timestamps, PIDs, kernel patch level — expected post-migration noise)
- Strict output schema with verdict ∈ {pass, warn, fail}
- Platform-aware terminology hint per call (Linux vs Windows)

The platform hint is the one runtime-injected piece of the
otherwise-static system prompt. See `_render_prompt` in
`client.py`.

---

## Where prompts live in code

| File | Prompt |
|------|--------|
| `app/core/llm/client.py:SYSTEM_PROMPT` | Validation system prompt |
| `app/core/strategy_planner.py:SYSTEM_PROMPT` | Strategy planner system prompt |
| `app/core/strategy_planner.py:build_user_prompt` | Strategy user message builder |
| `app/core/strategy_planner.py:_PRIMARY_GROUPING_FRAGMENTS` | Per-grouping prompt fragments |
| `app/core/strategy_planner.py:_RISK_FRAGMENTS` | Per-risk-approach fragments |
| `app/core/strategy_planner.py:_PRODUCTION_HANDLING_FRAGMENTS` | Per-prod-handling fragments |
| `app/core/strategy_planner.py:_APPLICATION_ATOMICITY_FRAGMENTS` | Per-atomicity fragments |
| `app/core/planner.py:PLANNER_SYSTEM_PROMPT` | Legacy single-shot planner prompt |
| `app/core/network_review.py:SYSTEM_PROMPT` | Network design review system prompt |
| `app/core/storage_review.py:SYSTEM_PROMPT` | Storage design review system prompt |
| `app/core/categorizer.py:SYSTEM_PROMPT` | Level 1 categorizer system prompt |

Every prompt is at module scope, not in a class — easier to grep,
easier to review in PRs without scrolling past business logic.
