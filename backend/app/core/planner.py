"""
Migration wave planner.

Sends VM baseline profiles to the local Ollama model and asks it to infer
each VM's role and dependencies, then group them into dependency-ordered
migration waves. All LLM calls go to the local Ollama instance — never to
external APIs.
"""

from __future__ import annotations

import json
from typing import Any

from app.core.llm.base import LLMBackend, LLMBackendError
from app.core.llm.factory import get_llm_backend

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


class PlannerError(RuntimeError):
    """Raised when the planner LLM call fails or returns unusable output."""


class MigrationPlanner:
    def __init__(
        self,
        backend: LLMBackend | None = None,
    ):
        self.backend = backend or get_llm_backend()

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
