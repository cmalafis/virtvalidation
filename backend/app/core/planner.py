"""
Migration wave planner.

Two-stage architecture (the LLM never sees raw VM collections):

  1. Mechanical pre-classification (Python, deterministic, fast).
     ``app.core.preclassifier.PreClassifier`` groups the input VMs by
     vCenter + target namespace + application_hint + role into M ≪ N
     candidate groups. M is bounded by ``llm_max_items_per_call``.

  2. LLM wave assignment (small input, ambiguous decision).
     The LLM receives ``M`` group summaries and decides their wave
     ordering. It NEVER sees raw vm_ids — the expansion from group →
     VMs is mechanical post-call, so the integrity checks ("every
     input vm_id placed in exactly one wave") pass by construction.

See ``docs/PLANNER_ARCHITECTURE.md`` for the full rationale and
``CLAUDE.md`` "LLM Input Discipline" for the architectural rule that
gates every future LLM-driven feature.

All LLM calls go to the configured backend (Ollama default; MockBackend
for tests) — never to external APIs.
"""

from __future__ import annotations

import json
from typing import Any

from app.core.llm.base import LLMBackend, LLMBackendError
from app.core.llm.factory import get_llm_backend
from app.core.preclassifier import PreClassifier, VMGroup
from app.models.vm import VM

_ALLOWED_RISK = {"low", "medium", "high"}

PLANNER_SYSTEM_PROMPT = """You are VirtValidate, an expert infrastructure architect
planning a VM migration from VMware to OpenShift Virtualization via the Migration
Toolkit for Virtualization (MTV / Forklift).

You will receive a list of VMs, each with:
  - vm_id (integer)
  - name, role hint (may be empty), os
  - a baseline profile: running services, open ports, mounts, DNS/interfaces
  - vsphere_networks: source portgroups the VM is attached to
  - vsphere_datastores: source datastores the VM's disks live on
  - target_namespace, target_storage_class, target_network_attachment: the
    destination context the operator already chose (may be empty)

Group VMs into ordered waves so each wave is a coherent migration batch.
Apply these grouping rules in order:

  1. Application dependencies. Everything a VM depends on must already be
     migrated in an earlier wave. Stateful services (databases, message
     brokers, storage backends) go first. Stateless app tiers follow.
     Edge (load balancers, reverse proxies, ingress) go last.

  2. Shared vSphere networks. VMs attached to the same source portgroup
     should migrate together in the same wave when possible. Splitting a
     portgroup across waves is a strong signal that east-west traffic will
     break mid-cutover, so prefer to keep them grouped unless dependency
     order forces a split.

  3. Shared vSphere datastores. VMs whose disks live on the same datastore
     should migrate together. Datastore I/O contention during a migration
     wave is real, but mixing datastores complicates rollback — prefer
     keeping a datastore's tenants in one wave when wave size permits.

  4. Risk profile. After grouping, estimate risk per wave:
       "low"    — isolated stateless VMs, dev/test, single-tenant datastore
       "medium" — app-tier with in-flight sessions, shared portgroup
       "high"   — stateful/shared-data VMs, multi-tenant datastore, edge

The "rationale" field for each wave MUST explain in plain English which
of the four rules above pulled these specific VMs into this wave (e.g.
"these three VMs share the DB Backend portgroup and the nfs-prod-fast
datastore, so they migrate as one cutover to keep east-west traffic and
storage I/O coherent").

Every vm_id from the input MUST appear in exactly one wave. Do not invent
vm_ids that were not provided.

Respond with a SINGLE JSON object and nothing else, matching this schema:
{
  "summary": "one-paragraph plain-English overview of the plan",
  "waves": [
    {
      "wave_number": 1,
      "vm_ids": [<int>, ...],
      "rationale": "why these VMs go together (cite networks/datastores/deps)",
      "estimated_risk": "low" | "medium" | "high"
    }
  ]
}
No markdown fences, no commentary."""


GROUP_PLANNER_SYSTEM_PROMPT = """You are VirtValidate, an expert infrastructure architect
planning a VM migration from VMware to OpenShift Virtualization via the Migration
Toolkit for Virtualization (MTV / Forklift).

HARD CONSTRAINTS — your output MUST satisfy ALL of these, or it will be rejected:

  1. EVERY group_id from the input MUST appear in EXACTLY ONE wave.
  2. NO group_id may appear in more than one wave.
  3. NO group_id may be omitted from your output.
  4. Wave numbers must be sequential integers starting from 1.
  5. Before responding, mentally enumerate your group_ids across all waves
     and verify the count equals the input count.

These constraints are non-negotiable. If you violate any, your output is
invalid and will be retried — re-read the input list and try again.

You will receive a small list of pre-formed VM GROUPS. Each group has already
been clustered mechanically by source vCenter, target namespace, network /
datastore overlap, application_hint, and role. Each group is described by:
  - id (string) — this is what you assign to waves
  - vm_count (integer; not exposed in your output)
  - role (web | app | data | edge | infrastructure | other)
  - state (stateful | stateless | unknown)
  - risk (low | medium | high)
  - depends_on (list of group ids this group likely depends on)
  - notes (one-line summary of the cohesion signal)

Wave ordering principles (apply when assigning):
  1. Dependencies first. Anything a group depends on MUST land in an earlier
     wave (lower wave_number). Data + infrastructure groups → first waves.
     App tiers → middle waves. Web + edge groups → last waves.
  2. Risk awareness. Group "high"-risk batches into smaller waves so
     rollback surface area stays bounded. "low"-risk batches can go in
     larger waves.
  3. State coupling. Stateful + infrastructure groups must complete
     before dependent stateless groups start their cutover.
  4. Keep wave count reasonable — 3 to 7 waves is the sweet spot.

The "rationale" for each wave MUST explain which principle pulled these
groups together.

Do not invent group_ids that weren't in the input. Do NOT include vm_ids
in your output — the orchestrator expands groups → vm_ids mechanically
after your reply.

Respond with a SINGLE JSON object and nothing else, matching this schema:
{
  "summary": "one-paragraph plain-English overview of the plan",
  "waves": [
    {
      "wave_number": 1,
      "group_ids": ["<group-id-1>", "<group-id-2>", ...],
      "rationale": "why these groups go together (cite the principle)",
      "estimated_risk": "low" | "medium" | "high"
    }
  ]
}
No markdown fences, no commentary."""


# Default number of LLM retry attempts before falling back to the
# deterministic assigner. Each retry includes the previous attempt's
# validation error in the prompt so a smaller model can self-correct.
_DEFAULT_MAX_LLM_ATTEMPTS = 3


_BATCH_WAVE_RATIONALE_SYSTEM_PROMPT = """You are writing operator-readable
rationale paragraphs for a VM migration plan's waves. The wave structure
has already been decided by deterministic Python rules — you do NOT
reassign groups, renumber waves, or reorder anything.

You will receive a small list of waves with their member groups. For
EACH wave, write a brief rationale (3-5 sentences) explaining:
  - Why these groups make sense together (shared cohesion signals — same
    role tier, same vCenter, same application).
  - Why this wave position relative to the others (dependencies
    satisfied, risk progression, HA spread).
  - Key risks operators should watch during cutover.
  - Any HA considerations (primary first, replica spread).

Respond with a SINGLE JSON object and nothing else, matching this schema:
{
  "rationales": [
    {"wave_number": 1, "rationale": "..."},
    {"wave_number": 2, "rationale": "..."}
  ]
}

Every wave_number from the input MUST appear EXACTLY ONCE in your
output. No markdown fences, no commentary."""


# Per CLAUDE.md "LLM Input Discipline": at most 10 items per LLM call.
# Plans with more than 10 waves split into multiple batches.
_RATIONALE_BATCH_SIZE = 10


def _render_batch_wave_rationale_prompt(waves) -> str:
    """Format a batch of waves' groups for one rationale LLM call.

    Each wave carries at most ``MAX_VMS_PER_WAVE`` groups (the wave
    skeleton enforces this), and a batch carries at most
    ``_RATIONALE_BATCH_SIZE`` waves, so the prompt stays comfortably
    within an 8K context budget.
    """
    expected = sorted(w.wave_number for w in waves)
    lines = [
        f"Generate rationale text for the following {len(waves)} wave(s).",
        f"Expected wave_numbers in your output: {expected}.",
        "",
    ]
    for wave in waves:
        lines.append(
            f"=== Wave {wave.wave_number} "
            f"({len(wave.groups)} group(s), risk={wave.estimated_risk}) ==="
        )
        for i, g in enumerate(wave.groups, start=1):
            lines.extend(
                [
                    f"  GROUP {i}: id={g.id!r}",
                    f"    - {len(g.vm_ids)} VMs",
                    f"    - role={g.estimated_role} "
                    f"state={g.estimated_state} risk={g.migration_risk}",
                    f"    - notes: {g.notes}",
                ]
            )
        lines.append("")
    lines.append("Produce the JSON object specified in the system prompt now.")
    return "\n".join(lines)


def _parse_batch_wave_rationale(
    raw: str,
    expected_wave_numbers: set[int],
) -> dict[int, str]:
    """Parse a batched rationale response into wave_number → text.

    Permissive: strips optional markdown fences, ignores unknown
    wave_numbers, drops empty strings. Missing wave_numbers are
    simply absent from the result — the caller falls back to the
    deterministic template for those waves.
    """
    raw = raw.strip()
    if raw.startswith("```"):
        # Some models still wrap JSON in fences despite the system
        # prompt's instructions; strip them defensively.
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    try:
        blob = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(blob, dict):
        return {}
    items = blob.get("rationales")
    if not isinstance(items, list):
        return {}
    out: dict[int, str] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        wn = item.get("wave_number")
        text = item.get("rationale")
        if (
            isinstance(wn, int)
            and wn in expected_wave_numbers
            and isinstance(text, str)
            and text.strip()
        ):
            out[wn] = text.strip()
    return out


def _template_rationale(wave) -> str:
    """Mechanical wave-level rationale (no LLM)."""
    roles = sorted({g.estimated_role for g in wave.groups})
    states = sorted({g.estimated_state for g in wave.groups})
    risk = wave.estimated_risk
    vm_count = wave.vm_count
    group_count = len(wave.groups)
    role_label = "/".join(roles) if roles else "mixed"
    state_label = ", ".join(states) if states else "mixed"
    return (
        f"Wave {wave.wave_number}: {vm_count} VMs across {group_count} "
        f"groups ({role_label} tier, {state_label}). Aggregate risk "
        f"{risk}. Cutover order follows dependency + role priority; HA "
        f"families are spread across waves so the original cluster keeps "
        "quorum during migration."
    )


class PlannerError(RuntimeError):
    """Raised when the planner LLM call fails or returns unusable output."""


class MigrationPlanner:
    def __init__(
        self,
        backend: LLMBackend | None = None,
        *,
        preclassifier: PreClassifier | None = None,
    ):
        self.backend = backend or get_llm_backend()
        self.preclassifier = preclassifier or PreClassifier()

    # ------------------------------------------------------------------
    # New two-stage flow — operates on groups, not raw VMs.
    # ------------------------------------------------------------------
    def plan_with_groups(
        self,
        vms: list[VM],
        *,
        max_llm_attempts: int = _DEFAULT_MAX_LLM_ATTEMPTS,
        ha_strategy: str = "spread",
        generate_rationale: bool = True,
    ) -> dict:
        """Generate a wave plan with mechanical wave assignment.

        New architecture (post-K/L/M refactor):

          1. Preclassifier (Python, unbounded output) — N VMs → M
             groups by vCenter / namespace / role / app_hint /
             network / datastore. No cap.
          2. HA spread / together / auto expansion (Python) — splits
             multi-member HA groups into per-member micro-groups
             when ha_strategy="spread" so the wave packer
             distributes them naturally.
          3. Mechanical wave skeleton (Python, deterministic) — the
             ``MechanicalWaveAssigner`` packs groups into waves
             respecting:
               * MAX_VMS_PER_WAVE (MTV concurrency limit, default 10)
               * MAX_HA_PEERS_PER_WAVE (quorum preservation, default 2)
               * Topological dependency order
               * Role priority (infrastructure → data → app → web → edge)
          4. Per-wave LLM rationale (small input, one wave at a
             time) — each LLM call sees at most ``MAX_VMS_PER_WAVE``
             groups, well under the per-call ceiling. Template
             rationale is used if the LLM fails.
          5. Integrity check (Python, belt-and-braces).

        The LLM no longer decides wave structure. Every wave
        placement is deterministic Python; the LLM only generates
        human-readable text for already-placed waves. That makes
        the planner reliable at any scale — failure modes are
        bounded to "rationale text missing" rather than "wave
        structure invalid".
        """
        if not vms:
            raise PlannerError("Cannot plan with zero VMs")

        groups = self.preclassifier.classify(vms)
        if not groups:
            raise PlannerError("Pre-classifier produced no groups")

        # HA strategy expansion. ``spread`` (default) splits HA-tagged
        # groups into per-member micro-groups so the wave packer
        # naturally distributes primary / replica / member across
        # consecutive waves — at any point during migration at least
        # one node still serves the original cluster. ``together``
        # keeps the original group intact. ``auto`` splits only
        # ≥3-member clusters.
        if ha_strategy in {"spread", "auto"}:
            expanded: list = []
            for g in groups:
                if ha_strategy == "auto":
                    ha_count = sum(1 for m in g.ha_members if m.ha_role != "standalone")
                    if ha_count < 3:
                        expanded.append(g)
                        continue
                expanded.extend(self.preclassifier.split_ha_group(g))
            groups = expanded

        provided_vm_ids = {vm.id for vm in vms}
        classified_vm_ids: set[int] = set()
        for g in groups:
            classified_vm_ids.update(g.vm_ids)
        if classified_vm_ids != provided_vm_ids:
            missing = provided_vm_ids - classified_vm_ids
            extra = classified_vm_ids - provided_vm_ids
            raise PlannerError(
                f"Pre-classifier dropped or invented vm_ids "
                f"(missing={sorted(missing)}, extra={sorted(extra)})"
            )

        # Stage 3: mechanical wave assignment. Importing locally to
        # avoid a circular import (preclassifier ← wave_skeleton ←
        # planner ← preclassifier).
        from app.core.wave_skeleton import MechanicalWaveAssigner

        assigner = MechanicalWaveAssigner()
        waves_struct = assigner.assign_waves(groups)
        if not waves_struct:
            raise PlannerError("Mechanical wave assigner produced no waves")

        # Stage 4: per-wave rationale. Each LLM call sees at most
        # one wave's groups (≤ MAX_VMS_PER_WAVE), so we stay under
        # the per-call ceiling regardless of overall plan size.
        rationale_method = "template"
        rationale_calls = 0
        if generate_rationale:
            rationale_method, rationale_calls = self._fill_wave_rationale(
                waves_struct,
                max_attempts=max_llm_attempts,
            )

        # Stage 5: expand to API shape + integrity check.
        groups_by_id = {g.id: g for g in groups}
        expanded_waves: list[dict] = []
        for wave in waves_struct:
            wave_vm_ids: list[int] = []
            for g in wave.groups:
                wave_vm_ids.extend(groups_by_id[g.id].vm_ids)
            expanded_waves.append(
                {
                    "wave_number": wave.wave_number,
                    "vm_ids": sorted(set(wave_vm_ids)),
                    "group_ids": [g.id for g in wave.groups],
                    "rationale": "\n\n".join(wave.notes) if wave.notes else "",
                    "estimated_risk": wave.estimated_risk,
                }
            )

        self._verify_expanded_plan(expanded_waves, provided_vm_ids)

        group_to_wave = {g.id: w.wave_number for w in waves_struct for g in w.groups}
        groups_api = []
        for g in groups:
            row = g.to_api_dict()
            row["wave_number"] = group_to_wave.get(g.id)
            groups_api.append(row)

        # Method telemetry: "mechanical" is the new default;
        # "mechanical+llm_rationale" when the LLM produced text on
        # at least one wave; "mechanical+template_rationale" when
        # every rationale used the template fallback.
        method = (
            "mechanical+llm_rationale"
            if rationale_method == "llm"
            else "mechanical+template_rationale"
        )

        return {
            "summary": (
                f"Migration plan: {len(vms)} VMs in {len(groups)} groups "
                f"across {len(waves_struct)} waves. Wave structure produced "
                f"deterministically by Python; rationale via "
                f"{'LLM' if rationale_method == 'llm' else 'template'}."
            ),
            "waves": expanded_waves,
            "groups": groups_api,
            "groups_formed": len(groups),
            "method": method,
            "attempts": rationale_calls,
        }

    def _fill_wave_rationale(
        self,
        waves_struct: list,
        *,
        max_attempts: int,
    ) -> tuple[str, int]:
        """Generate per-wave rationale text in batched LLM calls.

        Returns ``(method, total_llm_calls)``.

        Each LLM call covers up to ``_RATIONALE_BATCH_SIZE`` waves and
        returns a JSON map of wave_number → rationale. For typical
        plans (≤10 waves) this is a single call, replacing the prior
        loop of N sequential per-wave calls. ``max_attempts`` is
        accepted for caller back-compat but no longer used — a
        per-batch failure falls back to the deterministic template
        for every wave in that batch.
        """
        del max_attempts  # accepted for back-compat; no longer used
        import logging

        logger = logging.getLogger(__name__)
        if not waves_struct:
            return "template", 0
        any_llm = False
        calls = 0
        for start in range(0, len(waves_struct), _RATIONALE_BATCH_SIZE):
            batch = waves_struct[start : start + _RATIONALE_BATCH_SIZE]
            expected = {w.wave_number for w in batch}
            rationales_by_wn: dict[int, str] = {}
            try:
                user_prompt = _render_batch_wave_rationale_prompt(batch)
                raw = self._chat(
                    _BATCH_WAVE_RATIONALE_SYSTEM_PROMPT,
                    user_prompt,
                )
                calls += 1
                rationales_by_wn = _parse_batch_wave_rationale(raw, expected)
            except (PlannerError, LLMBackendError) as e:
                logger.warning(
                    "Batched rationale LLM call failed for waves %s "
                    "(%s); using template fallback for the batch.",
                    sorted(expected),
                    e,
                )
            for wave in batch:
                text = rationales_by_wn.get(wave.wave_number)
                if text:
                    wave.notes.append(text)
                    any_llm = True
                else:
                    wave.notes.append(_template_rationale(wave))
        method = "llm" if any_llm else "template"
        return method, calls

    # ------------------------------------------------------------------
    # Retry + fallback orchestration
    # ------------------------------------------------------------------
    def _assign_waves_with_retry(
        self,
        groups: list[VMGroup],
        *,
        max_attempts: int,
    ) -> tuple[dict, str, int]:
        """Try the LLM up to ``max_attempts`` times before falling back.

        Returns ``(wave_assignment, method, attempts_taken)``. ``method``
        is one of ``"llm"``, ``"llm_retry_<N>"``, or
        ``"mechanical_fallback"`` so the API can surface to operators
        which path produced their plan.
        """
        import logging

        logger = logging.getLogger(__name__)

        provided_ids = {g.id: g for g in groups}
        last_error: str | None = None
        for attempt in range(1, max_attempts + 1):
            user_prompt = self._render_groups_prompt(
                groups,
                previous_error=last_error,
            )
            try:
                raw = self._chat(GROUP_PLANNER_SYSTEM_PROMPT, user_prompt)
                wave_assignment = self._parse_group_plan(raw, set(provided_ids))
                method = "llm" if attempt == 1 else f"llm_retry_{attempt - 1}"
                return wave_assignment, method, attempt
            except PlannerError as e:
                last_error = str(e)
                logger.warning(
                    "Wave assignment attempt %d/%d failed: %s. "
                    "Retrying with corrective context.",
                    attempt,
                    max_attempts,
                    last_error,
                )
                continue

        # All attempts failed — fall through to deterministic assignment.
        # This is an architectural choice, not a failure mode: federal
        # customers should always get a plan, just one without LLM
        # rationale nuance.
        logger.error(
            "LLM wave assignment failed after %d attempts. Last error: %s. "
            "Falling back to mechanical assignment.",
            max_attempts,
            last_error,
        )
        wave_assignment = self._mechanical_assign_waves(groups, last_error=last_error)
        return wave_assignment, "mechanical_fallback", max_attempts

    @staticmethod
    def _render_groups_prompt(
        groups: list[VMGroup],
        *,
        previous_error: str | None = None,
    ) -> str:
        # The numbered + named format below makes it easier for smaller
        # models to track which groups they've placed — the LLM-prompt-
        # engineering literature consistently shows numbered lists
        # outperform raw JSON for "place each item exactly once"
        # tasks.
        lines: list[str] = []
        if previous_error:
            lines.extend(
                [
                    "PREVIOUS ATTEMPT FAILED with this error:",
                    f"  {previous_error}",
                    "",
                    "Your previous output violated the hard constraints listed in",
                    "the system prompt. Re-read the input list below, place every",
                    "group_id in EXACTLY ONE wave, and verify before responding.",
                    "",
                ]
            )
        lines.append(f"Assign the following {len(groups)} groups to migration waves:")
        lines.append("")
        for i, g in enumerate(groups, start=1):
            lines.extend(
                [
                    f"GROUP {i}: id={g.id!r}",
                    f"  - {len(g.vm_ids)} VMs",
                    f"  - role={g.estimated_role} state={g.estimated_state} risk={g.migration_risk}",
                    f"  - depends_on: {list(g.dependency_hints) or '(none)'}",
                    f"  - notes: {g.notes}",
                    "",
                ]
            )
        lines.append(
            "REMEMBER: each group_id appears in EXACTLY ONE wave. "
            f"Verify your output contains exactly {len(groups)} group_ids "
            "across all waves before responding."
        )
        lines.append("")
        # Also include the raw JSON body so callers that pin against
        # group_ids in their tests (MockBackend, validation paths) keep
        # working without re-parsing the human-readable lines.
        body = {"groups": [g.to_llm_dict() for g in groups]}
        lines.append("Machine-readable group list:")
        lines.append(json.dumps(body, indent=2, sort_keys=True))
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Mechanical fallback assignment
    # ------------------------------------------------------------------
    @staticmethod
    def _mechanical_assign_waves(
        groups: list[VMGroup],
        *,
        last_error: str | None = None,
    ) -> dict:
        """Topological-sort + role priority → deterministic waves.

        Used when every LLM attempt produces invalid output. The
        algorithm:

          1. Topo-sort groups by ``dependency_hints`` so each group
             lands AFTER every group it depends on.
          2. Assign each group to the earliest wave that comes after
             its latest dependency's wave (or wave 1 if no deps).
          3. Within a wave, order by (role-priority, risk).
          4. Generate template-based rationale from group attributes.

        Returns the same ``{summary, waves}`` shape the LLM path
        produces so the caller doesn't need to special-case anything.
        """
        ordered = MigrationPlanner._topological_sort(groups)
        assigned: dict[str, int] = {}
        wave_groups: dict[int, list[VMGroup]] = {}

        for group in ordered:
            min_wave = 1
            for dep_id in group.dependency_hints:
                if dep_id in assigned:
                    min_wave = max(min_wave, assigned[dep_id] + 1)
            assigned[group.id] = min_wave
            wave_groups.setdefault(min_wave, []).append(group)

        _role_priority = {
            "infrastructure": 0,
            "data": 1,
            "app": 2,
            "web": 3,
            "edge": 4,
            "other": 5,
        }
        _risk_priority = {"low": 0, "medium": 1, "high": 2}

        waves: list[dict] = []
        for wave_number in sorted(wave_groups):
            members = sorted(
                wave_groups[wave_number],
                key=lambda g: (
                    _role_priority.get(g.estimated_role, 5),
                    _risk_priority.get(g.migration_risk, 1),
                    g.id,
                ),
            )
            waves.append(
                {
                    "wave_number": wave_number,
                    "group_ids": [g.id for g in members],
                    "rationale": MigrationPlanner._describe_wave(
                        wave_number,
                        members,
                    ),
                    "estimated_risk": MigrationPlanner._wave_risk(members),
                }
            )

        summary = (
            "Mechanical fallback plan: LLM wave assignment unavailable, "
            "using deterministic role + dependency ordering."
        )
        if last_error:
            summary += f" (LLM last error: {last_error[:200]})"
        return {"summary": summary, "waves": waves}

    @staticmethod
    def _topological_sort(groups: list[VMGroup]) -> list[VMGroup]:
        """Kahn's algorithm over dependency_hints, stable by group.id.

        Cycles short-circuit by emitting the remaining groups in
        deterministic id order — the planner's depends_on is a hint,
        not a guarantee, so we don't refuse to plan on a cycle.
        """
        by_id = {g.id: g for g in groups}
        indegree: dict[str, int] = {g.id: 0 for g in groups}
        for g in groups:
            for dep in g.dependency_hints:
                if dep in indegree:
                    indegree[g.id] += 1
        # Stable processing — use id ordering when degrees tie so the
        # output is deterministic across runs.
        ready = sorted(gid for gid, deg in indegree.items() if deg == 0)
        ordered: list[VMGroup] = []
        while ready:
            ready.sort()
            gid = ready.pop(0)
            ordered.append(by_id[gid])
            # Decrement indegree of every group that depends on this id.
            for g in groups:
                if gid in g.dependency_hints and indegree[g.id] > 0:
                    indegree[g.id] -= 1
                    if indegree[g.id] == 0:
                        ready.append(g.id)
        # Cycle remainder — append any ungrouped ids in stable order.
        placed = {g.id for g in ordered}
        for gid in sorted(by_id):
            if gid not in placed:
                ordered.append(by_id[gid])
        return ordered

    @staticmethod
    def _describe_wave(wave_number: int, members: list[VMGroup]) -> str:
        roles = {g.estimated_role for g in members}
        states = {g.estimated_state for g in members}
        vm_count = sum(len(g.vm_ids) for g in members)
        group_count = len(members)
        if "infrastructure" in roles:
            return (
                f"Wave {wave_number}: Infrastructure migration "
                f"({vm_count} VMs across {group_count} groups). "
                f"AD, DNS, and PKI services migrate first to provide "
                f"foundation for subsequent waves."
            )
        if "data" in roles:
            return (
                f"Wave {wave_number}: Data tier migration "
                f"({vm_count} VMs across {group_count} groups). "
                f"Stateful services with high migration risk; ensure "
                f"backups before proceeding."
            )
        if {"web", "app"} <= roles:
            return (
                f"Wave {wave_number}: Application tier "
                f"({vm_count} VMs). Web and app servers migrated "
                f"together since they're stateless and inter-dependent."
            )
        if "web" in roles:
            return (
                f"Wave {wave_number}: Web tier ({vm_count} VMs). " f"Stateless front-end services."
            )
        if "app" in roles:
            return (
                f"Wave {wave_number}: Application tier ({vm_count} VMs). "
                f"Stateless application services."
            )
        if "edge" in roles:
            return (
                f"Wave {wave_number}: Edge tier ({vm_count} VMs). "
                f"Load balancers, ingress, and edge services."
            )
        state_desc = (
            "stateful"
            if "stateful" in states
            else ("stateless" if "stateless" in states else "mixed")
        )
        return (
            f"Wave {wave_number}: {vm_count} VMs across {group_count} "
            f"groups. Mixed services ({state_desc})."
        )

    @staticmethod
    def _wave_risk(members: list[VMGroup]) -> str:
        priority = {"low": 0, "medium": 1, "high": 2}
        highest = max((priority.get(g.migration_risk, 1) for g in members), default=1)
        return {0: "low", 1: "medium", 2: "high"}[highest]

    @staticmethod
    def _parse_group_plan(raw: str, provided_group_ids: set[str]) -> dict:
        try:
            plan: Any = json.loads(raw)
        except json.JSONDecodeError as e:
            raise PlannerError(f"Group planner output was not valid JSON: {e}\n{raw[:500]}") from e
        if not isinstance(plan, dict):
            raise PlannerError("Group planner output was not a JSON object")

        waves = plan.get("waves")
        if not isinstance(waves, list) or not waves:
            raise PlannerError("Group planner output missing non-empty 'waves' list")

        seen: set[str] = set()
        normalized: list[dict] = []
        for idx, wave in enumerate(waves, start=1):
            if not isinstance(wave, dict):
                raise PlannerError(f"Wave at index {idx - 1} is not an object")
            wave_number = wave.get("wave_number", idx)
            if not isinstance(wave_number, int):
                raise PlannerError(f"wave_number must be int, got {wave_number!r}")
            group_ids = wave.get("group_ids")
            if not isinstance(group_ids, list) or not group_ids:
                raise PlannerError(f"wave {wave_number}: group_ids must be a non-empty list")
            if not all(isinstance(g, str) for g in group_ids):
                raise PlannerError(f"wave {wave_number}: group_ids must all be strings")
            unknown = set(group_ids) - provided_group_ids
            if unknown:
                raise PlannerError(
                    f"wave {wave_number}: planner returned unknown group_ids {sorted(unknown)}"
                )
            duplicated = seen & set(group_ids)
            if duplicated:
                raise PlannerError(
                    f"wave {wave_number}: group_ids {sorted(duplicated)} appear in multiple waves"
                )
            seen.update(group_ids)

            risk = wave.get("estimated_risk")
            if risk not in _ALLOWED_RISK:
                raise PlannerError(
                    f"wave {wave_number}: estimated_risk must be one of {sorted(_ALLOWED_RISK)}, "
                    f"got {risk!r}"
                )
            rationale = wave.get("rationale", "")
            if not isinstance(rationale, str):
                raise PlannerError(f"wave {wave_number}: rationale must be a string")
            normalized.append(
                {
                    "wave_number": wave_number,
                    "group_ids": list(group_ids),
                    "rationale": rationale,
                    "estimated_risk": risk,
                }
            )

        missing = provided_group_ids - seen
        if missing:
            raise PlannerError(
                f"Group planner did not place group_ids {sorted(missing)} into any wave"
            )
        normalized.sort(key=lambda w: w["wave_number"])
        summary = plan.get("summary", "")
        if not isinstance(summary, str):
            summary = ""
        return {"summary": summary, "waves": normalized}

    @staticmethod
    def _verify_expanded_plan(waves: list[dict], provided_vm_ids: set[int]) -> None:
        seen: set[int] = set()
        for wave in waves:
            for vid in wave["vm_ids"]:
                if vid in seen:
                    raise PlannerError(
                        f"Expanded plan duplicated vm_id={vid} across waves "
                        "(bug in preclassifier or expansion logic)"
                    )
                seen.add(vid)
        missing = provided_vm_ids - seen
        if missing:
            raise PlannerError(
                f"Expanded plan did not place vm_ids {sorted(missing)} into any wave"
            )

    # ------------------------------------------------------------------
    # Legacy raw-VM flow — kept for explicit opt-out + small fleets.
    # Callers should prefer plan_with_groups().
    # ------------------------------------------------------------------
    def plan(self, vm_profiles: list[dict]) -> dict:
        """Generate a wave plan for the given VMs.

        vm_profiles is a list of dicts with at minimum: vm_id, name, role,
        os_family, and a baseline profile dict. The caller assembles these.
        """
        if not vm_profiles:
            raise PlannerError("Cannot plan with zero VMs")

        provided_ids = {p["vm_id"] for p in vm_profiles}
        user_prompt = self._render_prompt(vm_profiles)
        raw = self._chat(PLANNER_SYSTEM_PROMPT, user_prompt)
        return self._parse_plan(raw, provided_ids)

    def _chat(self, system: str, user: str) -> str:
        try:
            response = self.backend.chat_sync(
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=0.1,
            )
        except LLMBackendError as e:
            raise PlannerError(str(e)) from e
        content = response.get("content", "")
        if not content:
            raise PlannerError("LLM backend returned an empty message")
        return content

    @staticmethod
    def _render_prompt(vm_profiles: list[dict]) -> str:
        return (
            f"VMs to plan ({len(vm_profiles)} total):\n\n"
            f"{json.dumps(vm_profiles, indent=2, sort_keys=True, default=str)}\n"
        )

    @staticmethod
    def _parse_plan(raw: str, provided_ids: set[int]) -> dict:
        try:
            plan: Any = json.loads(raw)
        except json.JSONDecodeError as e:
            raise PlannerError(f"Planner output was not valid JSON: {e}\n{raw[:500]}") from e

        if not isinstance(plan, dict):
            raise PlannerError("Planner output was not a JSON object")

        waves = plan.get("waves")
        if not isinstance(waves, list) or not waves:
            raise PlannerError("Planner output missing non-empty 'waves' list")

        seen_ids: set[int] = set()
        normalized_waves: list[dict] = []
        for idx, wave in enumerate(waves, start=1):
            if not isinstance(wave, dict):
                raise PlannerError(f"Wave at index {idx - 1} is not an object")

            wave_number = wave.get("wave_number", idx)
            if not isinstance(wave_number, int):
                raise PlannerError(f"wave_number must be an int, got {wave_number!r}")

            vm_ids = wave.get("vm_ids")
            if not isinstance(vm_ids, list) or not vm_ids:
                raise PlannerError(f"wave {wave_number}: vm_ids must be a non-empty list")
            if not all(isinstance(v, int) for v in vm_ids):
                raise PlannerError(f"wave {wave_number}: vm_ids must all be ints")

            unknown = set(vm_ids) - provided_ids
            if unknown:
                raise PlannerError(
                    f"wave {wave_number}: planner returned unknown vm_ids {sorted(unknown)}"
                )
            duplicated = seen_ids & set(vm_ids)
            if duplicated:
                raise PlannerError(
                    f"wave {wave_number}: vm_ids {sorted(duplicated)} appear in multiple waves"
                )
            seen_ids.update(vm_ids)

            risk = wave.get("estimated_risk")
            if risk not in _ALLOWED_RISK:
                raise PlannerError(
                    f"wave {wave_number}: estimated_risk must be one of {sorted(_ALLOWED_RISK)}, "
                    f"got {risk!r}"
                )

            rationale = wave.get("rationale", "")
            if not isinstance(rationale, str):
                raise PlannerError(f"wave {wave_number}: rationale must be a string")

            normalized_waves.append(
                {
                    "wave_number": wave_number,
                    "vm_ids": vm_ids,
                    "rationale": rationale,
                    "estimated_risk": risk,
                }
            )

        missing = provided_ids - seen_ids
        if missing:
            raise PlannerError(f"planner did not place vm_ids {sorted(missing)} into any wave")

        normalized_waves.sort(key=lambda w: w["wave_number"])

        summary = plan.get("summary", "")
        if not isinstance(summary, str):
            summary = ""

        return {"summary": summary, "waves": normalized_waves}
