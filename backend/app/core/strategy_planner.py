"""Strategy-driven migration planner.

Replaces the single-shot prompt in :mod:`app.core.planner` with a
prompt builder that takes a structured :class:`PlanningStrategy`
plus a freeform-constraints text block and produces a much richer
LLM call:

  - Every wizard choice (primary grouping, wave sizing, risk
    approach, prod handling, application atomicity) lands in the
    prompt as an explicit instruction.
  - The freeform constraints field rides through verbatim so the
    LLM can reason about customer-specific quirks ("no migrations
    in March", "RAC cluster must stay together").
  - The expected output schema includes per-wave rationale,
    plan-level warnings, and a next_actions checklist — the
    consultative output federal customers pay for.

The existing :class:`app.core.planner.MigrationPlanner` is kept for
backward compatibility (older API tests and plans created before the
wizard landed go through it). New strategy-driven plans go through
:class:`StrategyPlanner` here.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from app.core.llm.base import LLMBackend, LLMBackendError
from app.core.llm.factory import get_llm_backend
from app.models.plan import (
    ApplicationAtomicity,
    PlanningStrategy,
    PrimaryGrouping,
    ProductionHandling,
    RiskApproach,
    WaveSizeTarget,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Prompt fragments — one per wizard choice. Keeping them as a dispatch
# table (not inlined) makes it easy to add a new strategy option later
# without touching the prompt assembler.
# ---------------------------------------------------------------------------

_PRIMARY_GROUPING_FRAGMENTS: dict[PrimaryGrouping, str] = {
    PrimaryGrouping.application: (
        "GROUP BY APPLICATION. Cluster VMs that belong to the same logical "
        "application together. Use the application_hint field on each VM "
        "as the primary signal; fall back to naming patterns when missing."
    ),
    PrimaryGrouping.vcenter_folder: (
        "GROUP BY vCENTER FOLDER STRUCTURE. Use the source vCenter folder "
        "as the wave boundary — folders typically reflect organizational "
        "or operational structure that customers want preserved through "
        "migration."
    ),
    PrimaryGrouping.application_owner: (
        "GROUP BY APPLICATION OWNER. Cluster VMs by the owner field so "
        "each wave touches a small number of teams. Minimizes "
        "cross-team coordination per wave."
    ),
    PrimaryGrouping.environment: (
        "GROUP BY ENVIRONMENT. Sequence dev → staging → prod. Migrate "
        "every dev VM before moving any staging VM, every staging before "
        "any prod. Classic conservative approach."
    ),
    PrimaryGrouping.business_unit: (
        "GROUP BY BUSINESS UNIT. Use organizational boundaries — VMs "
        "owned by the same BU travel together. Best for matrix orgs "
        "where a single BU owns its cutover window."
    ),
    PrimaryGrouping.data_classification: (
        "GROUP BY DATA CLASSIFICATION. Migrate VMs at the same "
        "classification level together. Required for federal "
        "multi-classification environments — never mix unclassified "
        "and CUI/SECRET in one wave."
    ),
    PrimaryGrouping.llm_decides: (
        "AUTO-GROUP. Analyze the VM inventory and propose optimal "
        "grouping based on patterns you identify. Explain in the "
        "plan_summary which signals drove your choice."
    ),
}

_WAVE_SIZE_GUIDANCE: dict[WaveSizeTarget, str] = {
    WaveSizeTarget.small_5_10: "5-10 VMs per wave (slow but safe)",
    WaveSizeTarget.medium_10_20: "10-20 VMs per wave (balanced)",
    WaveSizeTarget.large_20_50: "20-50 VMs per wave (aggressive)",
    WaveSizeTarget.custom: "operator-specified custom size",
}

_RISK_FRAGMENTS: dict[RiskApproach, str] = {
    RiskApproach.low_first: (
        "LOW-RISK FIRST. Order waves so the team builds confidence "
        "with simple migrations before tackling complex ones. Stateless "
        "single-instance VMs early; clustered/stateful late."
    ),
    RiskApproach.high_first: (
        "HIGH-RISK FIRST. Tackle complex migrations first while team "
        "focus is fresh. Database clusters, multi-NIC apps, AD-joined "
        "workloads in early waves; simpler cleanup waves at the end."
    ),
    RiskApproach.mixed: (
        "MIXED PER WAVE. Each wave should include a balance of easy "
        "and complex VMs so progress is steady throughout. Avoid "
        "front-loading complexity or saving it all for the end."
    ),
}

_PRODUCTION_HANDLING_FRAGMENTS: dict[ProductionHandling, str] = {
    ProductionHandling.mixed: (
        "PROD/NON-PROD CAN MIX. The customer is comfortable with "
        "production and non-production VMs in the same wave."
    ),
    ProductionHandling.non_prod_first: (
        "NON-PROD FIRST. ALL non-production VMs (dev, staging, test) "
        "must migrate BEFORE any production VM. Use the environment "
        "field on each VM to determine prod vs non-prod; assume "
        "anything tagged 'prod', 'production', or unclear is prod."
    ),
    ProductionHandling.prod_dedicated_waves: (
        "PROD DEDICATED. Production VMs require their own waves with "
        "no non-production VMs mixed in. Non-production waves must "
        "come first; production waves follow with explicit rationale."
    ),
}

_APPLICATION_ATOMICITY_FRAGMENTS: dict[ApplicationAtomicity, str] = {
    ApplicationAtomicity.all_together: (
        "APPLICATION ATOMICITY = STRICT. All VMs of one application "
        "MUST land in the same wave. Simpler coordination, more "
        "downtime per app. If an application's size exceeds the wave "
        "target, exceed the size target rather than split the app, "
        "and warn about it in plan warnings."
    ),
    ApplicationAtomicity.can_split: (
        "APPLICATION ATOMICITY = FLEXIBLE. An application's VMs can "
        "span multiple waves when that enables gradual cutover or "
        "redundancy preservation. Document the split rationale in "
        "the wave's considerations field."
    ),
    ApplicationAtomicity.per_app_choice: (
        "APPLICATION ATOMICITY = PER-APP. For each application, "
        "decide based on its architecture: stateless app tiers can "
        "split; stateful clusters (DB, MQ, cache) stay together. "
        "Document your reasoning in the wave's considerations field."
    ),
}


# ---------------------------------------------------------------------------
# Output validation enums
# ---------------------------------------------------------------------------
_ALLOWED_RISK = {"low", "medium", "high"}


class StrategyPlannerError(RuntimeError):
    """Raised when the strategy-driven planner fails to call the LLM,
    parse the response, or validate the resulting plan."""


# ---------------------------------------------------------------------------
# Prompt assembly
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """You are VirtValidate, an expert infrastructure architect producing a
migration plan for a VMware → OpenShift Virtualization (KubeVirt via MTV /
Forklift) cutover. Your output is consultative — operators will read your
rationale to decide whether to proceed, adjust, or push back.

You will receive:
  - the customer's structured strategy choices (primary grouping, wave
    sizing, risk approach, production handling, application atomicity)
  - the customer's freeform constraints in plain English
  - a VM inventory with name, role, environment, owner, application_hint,
    vsphere_networks, vsphere_datastores

You MUST honor every structured strategy choice. The freeform constraints
section rides through as plain English — interpret it pragmatically and
flag anything you cannot satisfy via a plan-level warning.

Every wave MUST include rationale that explains:
  - why these VMs are grouped together
  - why this wave comes before/after others
  - what customer-stated criteria drove the choice
  - any tradeoffs you made

If you cannot satisfy a strategy + constraints combination cleanly, produce
the closest plan you can AND document the gap in the plan-level warnings.
DO NOT silently violate a constraint.

Respond with a SINGLE JSON object and nothing else. No markdown fences. Schema:

{
  "plan_summary": "two-or-three-sentence plain-English overview of the approach",
  "rationale": "longer paragraph explaining how this plan honors the customer's strategy and constraints",
  "warnings": [
    "plain-English warnings about tradeoffs or constraint conflicts you encountered"
  ],
  "waves": [
    {
      "wave_number": 1,
      "name": "Wave 1: Foundational Infrastructure",
      "vm_ids": [<int>, ...],
      "rationale": "specific reason these VMs are in this wave at this point in the sequence",
      "estimated_duration": "human-readable estimate (e.g. '4-6 hours')",
      "risk_level": "low" | "medium" | "high",
      "considerations": "anything operators should know before running this wave",
      "applications_included": ["list", "of", "application", "names"],
      "applications_split_warning": "non-empty string when an application has been split across this and another wave; null otherwise"
    }
  ],
  "next_actions": [
    "checklist items the operator should do before/after generating this plan"
  ]
}

Every vm_id from the input MUST appear in exactly one wave. Do not invent
vm_ids that were not provided. Order waves by wave_number starting at 1.
"""


def _wave_size_guidance(strategy: PlanningStrategy) -> str:
    """Return a single sentence telling the LLM the wave-size target."""
    if (
        strategy.wave_size_target == WaveSizeTarget.custom
        and strategy.wave_size_custom is not None
    ):
        return f"approximately {strategy.wave_size_custom} VMs per wave"
    return _WAVE_SIZE_GUIDANCE.get(
        strategy.wave_size_target, _WAVE_SIZE_GUIDANCE[WaveSizeTarget.medium_10_20]
    )


def build_user_prompt(
    strategy: PlanningStrategy, vm_profiles: list[dict]
) -> str:
    """Assemble the user-message body for one strategy-driven plan call."""
    grouping = _PRIMARY_GROUPING_FRAGMENTS.get(
        strategy.primary_grouping,
        _PRIMARY_GROUPING_FRAGMENTS[PrimaryGrouping.application],
    )
    risk = _RISK_FRAGMENTS.get(
        strategy.risk_approach, _RISK_FRAGMENTS[RiskApproach.mixed]
    )
    prod = _PRODUCTION_HANDLING_FRAGMENTS.get(
        strategy.production_handling,
        _PRODUCTION_HANDLING_FRAGMENTS[ProductionHandling.non_prod_first],
    )
    atomicity = _APPLICATION_ATOMICITY_FRAGMENTS.get(
        strategy.application_atomicity,
        _APPLICATION_ATOMICITY_FRAGMENTS[ApplicationAtomicity.all_together],
    )
    wave_size = _wave_size_guidance(strategy)

    freeform = (strategy.freeform_constraints or "").strip()
    freeform_block = (
        f"## Customer freeform constraints (interpret pragmatically)\n\n{freeform}\n\n"
        if freeform
        else "## Customer freeform constraints\n\n(none provided)\n\n"
    )

    return (
        f"## Customer strategy choices\n\n"
        f"- Primary grouping: {grouping}\n"
        f"- Wave size target: {wave_size}\n"
        f"- Risk approach: {risk}\n"
        f"- Production handling: {prod}\n"
        f"- Application atomicity: {atomicity}\n\n"
        f"{freeform_block}"
        f"## VM inventory ({len(vm_profiles)} VMs)\n\n"
        f"```json\n{json.dumps(vm_profiles, indent=2, sort_keys=True, default=str)}\n```\n\n"
        f"Produce the JSON object specified in the system prompt now."
    )


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------
class StrategyPlanner:
    """Strategy-driven plan generator.

    Constructed with an :class:`LLMBackend`; defaults to the factory
    so production code doesn't have to know which backend is wired.
    """

    def __init__(self, backend: Optional[LLMBackend] = None) -> None:
        self.backend = backend or get_llm_backend()

    def plan(
        self,
        strategy: PlanningStrategy,
        vm_profiles: list[dict],
    ) -> dict:
        """Generate a plan for the given strategy + VM inventory.

        Returns a dict with ``plan_summary`` / ``rationale`` /
        ``warnings`` / ``waves`` / ``next_actions`` plus the original
        prompt + raw response (so the API layer can persist them for
        federal audit). Raises :class:`StrategyPlannerError` on any
        failure — caller is responsible for marking the task as failed.
        """
        if not vm_profiles:
            raise StrategyPlannerError("Cannot plan with zero VMs")

        provided_ids = {p["vm_id"] for p in vm_profiles}
        user_prompt = build_user_prompt(strategy, vm_profiles)
        try:
            response = self.backend.chat_sync(
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.1,
            )
        except LLMBackendError as e:
            raise StrategyPlannerError(f"LLM backend failure: {e}") from e

        raw = response.get("content", "")
        if not raw:
            raise StrategyPlannerError("LLM returned empty content")

        plan = self._parse_and_validate(raw, provided_ids)
        plan["generation_prompt"] = user_prompt
        plan["generation_response"] = raw
        plan["model"] = response.get("model") or self.backend.default_model or ""
        return plan

    @staticmethod
    def _parse_and_validate(raw: str, provided_ids: set[int]) -> dict:
        """Parse the LLM's JSON output and validate plan integrity.

        Validation rules — every one of these is a real production-day
        bug if the LLM gets it wrong, so we reject hard rather than
        sweep under the rug:

          - JSON must be well-formed.
          - Top level is an object with required keys.
          - Every wave has wave_number, vm_ids, rationale, risk_level.
          - rationale is non-empty (the consultative deliverable).
          - Every input vm_id appears in EXACTLY ONE wave.
          - No invented vm_ids.
          - risk_level is in {low, medium, high}.
        """
        try:
            payload: Any = json.loads(raw)
        except json.JSONDecodeError as e:
            raise StrategyPlannerError(
                f"Plan output was not valid JSON: {e}\n{raw[:500]}"
            ) from e
        if not isinstance(payload, dict):
            raise StrategyPlannerError("Plan output was not a JSON object")

        waves_raw = payload.get("waves")
        if not isinstance(waves_raw, list) or not waves_raw:
            raise StrategyPlannerError("Plan missing non-empty 'waves' list")

        seen_ids: set[int] = set()
        normalized_waves: list[dict] = []
        for idx, wave in enumerate(waves_raw, start=1):
            if not isinstance(wave, dict):
                raise StrategyPlannerError(
                    f"Wave at index {idx - 1} is not an object"
                )
            wave_number = wave.get("wave_number", idx)
            if not isinstance(wave_number, int):
                raise StrategyPlannerError(
                    f"wave_number must be an int, got {wave_number!r}"
                )

            vm_ids_raw = wave.get("vm_ids") or wave.get("vms")
            if not isinstance(vm_ids_raw, list) or not vm_ids_raw:
                raise StrategyPlannerError(
                    f"Wave {wave_number}: vm_ids must be a non-empty list"
                )
            if not all(isinstance(v, int) for v in vm_ids_raw):
                raise StrategyPlannerError(
                    f"Wave {wave_number}: vm_ids must all be ints"
                )

            unknown = set(vm_ids_raw) - provided_ids
            if unknown:
                raise StrategyPlannerError(
                    f"Wave {wave_number}: planner returned unknown vm_ids "
                    f"{sorted(unknown)}"
                )
            duplicates = seen_ids & set(vm_ids_raw)
            if duplicates:
                raise StrategyPlannerError(
                    f"Wave {wave_number}: vm_ids {sorted(duplicates)} appear in "
                    "multiple waves"
                )
            seen_ids.update(vm_ids_raw)

            risk = (wave.get("risk_level") or "").lower()
            if risk not in _ALLOWED_RISK:
                raise StrategyPlannerError(
                    f"Wave {wave_number}: risk_level must be one of "
                    f"{sorted(_ALLOWED_RISK)}, got {risk!r}"
                )

            rationale = (wave.get("rationale") or "").strip()
            if not rationale:
                raise StrategyPlannerError(
                    f"Wave {wave_number}: rationale must be non-empty — "
                    "the consultative output is the whole point of "
                    "strategy-driven planning."
                )

            normalized_waves.append(
                {
                    "wave_number": wave_number,
                    "name": (wave.get("name") or f"Wave {wave_number}").strip(),
                    "vm_ids": list(vm_ids_raw),
                    "rationale": rationale,
                    "estimated_duration": (
                        wave.get("estimated_duration") or ""
                    ).strip(),
                    "risk_level": risk,
                    "considerations": (
                        wave.get("considerations") or ""
                    ).strip(),
                    "applications_included": list(
                        wave.get("applications_included") or []
                    ),
                    "applications_split_warning": (
                        wave.get("applications_split_warning") or None
                    ),
                    # Legacy field name for back-compat with existing
                    # MTV-YAML and per-wave report endpoints.
                    "estimated_risk": risk,
                }
            )

        missing = provided_ids - seen_ids
        if missing:
            raise StrategyPlannerError(
                f"Planner did not place vm_ids {sorted(missing)} into any wave"
            )

        normalized_waves.sort(key=lambda w: w["wave_number"])

        # Plan-level fields. We accept missing-but-empty for everything
        # except waves; the prompt encourages non-empty values but the
        # planner shouldn't blow up if the LLM produced bare bones.
        warnings = payload.get("warnings") or []
        if not isinstance(warnings, list):
            warnings = []
        next_actions = payload.get("next_actions") or []
        if not isinstance(next_actions, list):
            next_actions = []

        return {
            "plan_summary": (payload.get("plan_summary") or "").strip(),
            "rationale": (payload.get("rationale") or "").strip(),
            "warnings": [str(w) for w in warnings],
            "next_actions": [str(a) for a in next_actions],
            "waves": normalized_waves,
        }
