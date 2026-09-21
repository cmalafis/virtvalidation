"""End-to-end migration plan pipeline.

Owns the deterministic stages (0-5 + 7) and delegates Stage 6 to
``app.core.wave_annotation``. The pipeline runs sync where possible —
the only async piece is Stage 6 (parallel LLM calls).

Stage layout (see plan ``velvet-percolating-stream.md`` for the full
spec):

  - Stage 0 — mapping validation. Returns 422 with gap detail.
  - Stage 1+2 — partition + sub-partition (PreClassifier).
  - Stage 3 — HA family anti-affinity split (family.py).
  - Stage 4 — wave packing (MechanicalWaveAssigner).
  - Stage 5 — concurrency-group assignment.
  - Stage 6 — per-wave LLM annotation (parallel, validate-retry-fallback).
  - Stage 7 — MTV YAML emission per wave.

Stages 1-5 + 7 must complete for 250 VMs in well under a second on a
laptop. Stage 6 is parallelized so the wall-clock is dominated by the
slowest single LLM call, not their sum.

The output is a :class:`PlanPipelineResult` carrying annotated waves
(structured description / risk_score / risk_rationale /
notable_concerns / mtv_yaml / concurrency_group_id / method) plus the
pre-form groups for the UI to render.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from app.core.concurrency import assign_concurrency_groups
from app.core.family import split_overconcentrated_families
from app.core.mapping_validation import (
    MappingGap,
    ValidationResult,
    validate_plan_inputs,
)
from app.core.mtv import (
    MappingResolver,
    MTVGenerationError,
    WaveContext,
    generate_wave_yaml,
)
from app.core.preclassifier import PreClassifier, VMGroup
from app.core.wave_skeleton import MechanicalWaveAssigner, Wave
from app.models.target import ResourceMapping
from app.models.vm import VM

logger = logging.getLogger(__name__)


class PlanValidationError(ValueError):
    """Raised by Stage 0 when mapping coverage is incomplete."""

    def __init__(self, result: ValidationResult) -> None:
        self.result = result
        super().__init__(result.render())


@dataclass
class AnnotatedWave:
    """A wave plus its Stage-6 annotation and Stage-7 YAML.

    The structured annotation fields are populated by Stage 6 (or its
    mechanical fallback). The YAML field is populated by Stage 7.
    The pipeline never returns a wave without all of these set —
    every operator-facing wave is rendered with full context.

    ``vm_names`` is parallel-indexed to ``wave.vm_ids`` and is
    populated by the pipeline before serialization so the frontend
    can render hostnames without a separate /api/vms round-trip.
    Operators recognize ``backup-s-app-013.corp.local``; ``vm-2243``
    is just a row id.
    """

    wave: Wave
    description: str = ""
    risk_score: int = 3
    risk_rationale: str = ""
    notable_concerns: list[str] = field(default_factory=list)
    method: str = "mechanical_fallback"  # llm | llm_retry_N | mechanical_fallback
    mtv_yaml: str = ""
    vm_names: list[str] = field(default_factory=list)

    # Inference-capture fields — populated by Stage 6 so the pipeline's
    # caller (which holds the DB session) can write one InferenceLog row per
    # wave. Not serialized into MigrationPlan.waves[] (see to_dict); they're
    # transient capture, not operator-facing plan content.
    inference_messages: list[dict] | None = None
    inference_response: str | None = None
    inference_backend_type: str | None = None
    inference_model: str = ""
    inference_latency_ms: int = 0
    # Migratability findings across this wave's VMs, rolled up by rule —
    # [{id, category, label, vm_count, vm_names}]. Deterministic (from
    # app.core.assessment); never produced or seen by the LLM.
    considerations: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Render to the JSON shape persisted in MigrationPlan.waves[]."""
        return {
            "wave_number": self.wave.wave_number,
            "vm_ids": list(self.wave.vm_ids),
            "vm_names": list(self.vm_names),
            "group_ids": [g.key.as_string() for g in self.wave.groups],
            "vm_count": self.wave.vm_count,
            "estimated_risk": self.wave.estimated_risk,
            "concurrency_group_id": self.wave.concurrency_group_id,
            "description": self.description,
            "risk_score": self.risk_score,
            "risk_rationale": self.risk_rationale,
            "notable_concerns": list(self.notable_concerns),
            "method": self.method,
            "considerations": list(self.considerations),
            "rationale": self.description,  # legacy field name; kept for UI back-compat
            "mtv_yaml_available": bool(self.mtv_yaml),
        }


@dataclass
class PlanPipelineResult:
    """End-to-end pipeline output. ``method_per_wave`` lets operators
    debug LLM-quality regressions from the audit log."""

    waves: list[AnnotatedWave]
    groups: list[VMGroup]
    method_per_wave: dict[int, str] = field(default_factory=dict)


def _vm_name_lookup(vms: list[VM]) -> dict[int, str]:
    return {vm.id: vm.name for vm in vms}


def _wave_considerations(vms: list[VM], migration_type: str) -> list[dict[str, Any]]:
    """What the operator must know about this wave before starting it:
    each migratability finding present, and which VMs carry it. Warm-only
    findings are dropped from a cold plan, where they don't apply."""
    rollup: dict[str, dict[str, Any]] = {}
    for vm in vms:
        for f in (vm.assessment or {}).get("findings") or []:
            if f.get("applies_to") == "warm" and migration_type != "warm":
                continue
            row = rollup.setdefault(
                f["id"],
                {"id": f["id"], "category": f["category"], "label": f["label"], "vm_names": []},
            )
            row["vm_names"].append(vm.name)
    order = {"Critical": 0, "Warning": 1, "Information": 2}
    out = sorted(rollup.values(), key=lambda r: (order.get(r["category"], 9), r["id"]))
    for row in out:
        row["vm_count"] = len(row["vm_names"])
    return out


def _vm_payload(vm: VM) -> dict[str, Any]:
    """Shape the MTV emitter expects per VM (a flat dict, not the ORM row).

    ``target_namespace`` carries the per-VM ``target_namespace_override``
    so the MTV resolver's namespace-strategy fallback lands on the
    operator's declared namespace. Storage and network targets are
    resolved from the mapping; there's no per-VM fallback anymore.
    """
    return {
        "name": vm.name,
        "moref": vm.moref or "",
        "vsphere_networks": list(vm.vsphere_networks or []),
        "vsphere_datastores": list(vm.vsphere_datastores or []),
        "environment": vm.environment or "",
        "application_hint": vm.application_hint or "",
        "target_namespace": vm.target_namespace_override or "",
        "vcenter_folder": vm.vsphere_folder or "",
    }


def _build_resolver(mapping: ResourceMapping | None) -> MappingResolver | None:
    if mapping is None:
        return None
    return MappingResolver(
        network_mappings=list(mapping.network_mappings or []),
        storage_mappings=list(mapping.storage_mappings or []),
        namespace_mappings=mapping.namespace_mappings or [],
    )


def _mapping_for_wave(
    wave: Wave,
    mappings: list[ResourceMapping],
    vm_by_id: dict[int, VM],
) -> ResourceMapping | None:
    """Pick the mapping whose vCenter covers this wave's VMs.

    Per the partition rule, every VM in a wave shares one
    ``source_vcenter_id`` — so the lookup is unambiguous and we can
    return the first match. Returns None if the wave's VMs have no
    vcenter (legacy data) or no mapping in the list covers it.
    """
    if not mappings:
        return None
    for vid in wave.vm_ids:
        vm = vm_by_id.get(vid)
        if vm is None or vm.source_vcenter_id is None:
            continue
        for m in mappings:
            if m.vcenter_source_id == vm.source_vcenter_id:
                return m
        return None
    return None


def emit_wave_yaml(
    plan_id: int,
    wave: Wave,
    description: str,
    vm_by_id: dict[int, VM],
    resolver: MappingResolver | None,
    migration_type: str = "cold",
) -> str:
    """Stage 7 — render one wave's MTV YAML.

    Wraps ``app.core.mtv.generate_wave_yaml``. If a wave references
    VMs that aren't in ``vm_by_id``, raises ``MTVGenerationError``
    rather than emitting placeholder YAML — Stage 0 should have
    caught this; if we hit it here it's a programmer error worth a
    loud failure.
    """
    vm_payloads: list[dict[str, Any]] = []
    missing: list[int] = []
    for vid in wave.vm_ids:
        vm = vm_by_id.get(vid)
        if vm is None:
            missing.append(vid)
            continue
        vm_payloads.append(_vm_payload(vm))
    if missing:
        raise MTVGenerationError(f"Wave {wave.wave_number} references unknown vm_ids: {missing}")

    ctx = WaveContext.from_settings(
        plan_id=plan_id,
        wave_number=wave.wave_number,
        rationale=description,
        migration_type=migration_type,
    )
    return generate_wave_yaml(ctx, vm_payloads, resolver=resolver)


async def run_pipeline(
    vms: list[VM],
    mappings: list[ResourceMapping],
    *,
    plan_id: int,
    backend=None,
    max_llm_attempts: int = 3,
    progress_cb=None,
    migration_type: str = "cold",
) -> PlanPipelineResult:
    """Walk the seven stages and return an end-to-end annotated plan.

    The ``progress_cb`` is invoked with the current stage name before
    each stage's work begins, so callers can update a progress field
    on the Plan row without baking a status state machine into this
    module.

    ``backend`` is the LLM backend used by Stage 6. When ``None`` the
    pipeline still runs but every wave's ``method`` is
    ``"mechanical_fallback"`` — useful for tests of the deterministic
    layers in isolation.
    """
    # Lazy import — wave_annotation may import this module's
    # AnnotatedWave for typing, so avoid the cycle at top-level.
    from app.core.wave_annotation import annotate_waves

    def _progress(stage: str) -> None:
        if progress_cb is not None:
            try:
                progress_cb(stage)
            except Exception:  # noqa: BLE001 — progress reporting must not break the pipeline
                logger.exception("progress callback raised")

    # Stage 0 — validate mapping coverage.
    _progress("validating")
    coverage = validate_plan_inputs(vms, mappings)
    if not coverage.ok:
        raise PlanValidationError(coverage)

    # Stages 1+2 — partition + sub-partition.
    _progress("partitioning")
    classifier = PreClassifier()
    groups = classifier.classify(vms)

    # Stage 3 — HA family anti-affinity split.
    _progress("splitting")
    name_lookup = _vm_name_lookup(vms)
    groups = split_overconcentrated_families(groups, name_lookup)

    # Stage 4 — pack into waves.
    _progress("packing")
    assigner = MechanicalWaveAssigner()
    waves = assigner.assign_waves(groups)

    # Stage 5 — concurrency analysis.
    _progress("analyzing_concurrency")
    assign_concurrency_groups(waves, name_lookup)

    # Stage 6 — parallel LLM annotation (validate-retry-fallback).
    # ``name_lookup`` is reused: built once at Stage 3, threaded into
    # the annotator so the prompt builder can render operator-readable
    # sample_vm_names instead of partition keys or raw vm_ids.
    _progress("annotating")
    annotated = await annotate_waves(
        waves,
        backend=backend,
        max_attempts=max_llm_attempts,
        vm_name_by_id=name_lookup,
    )

    # Stage 7 — emit MTV YAML per wave + stamp parallel-indexed vm_names
    # so the frontend can render hostnames without a /api/vms join.
    # Each wave gets the mapping whose vCenter matches the wave's VMs;
    # per the partition rule, exactly one mapping is in play per wave.
    _progress("emitting_yaml")
    vm_by_id = {vm.id: vm for vm in vms}
    for aw in annotated:
        aw.vm_names = [name_lookup.get(vid, f"vm-{vid}") for vid in aw.wave.vm_ids]
        aw.considerations = _wave_considerations(
            [vm_by_id[vid] for vid in aw.wave.vm_ids if vid in vm_by_id], migration_type
        )
        wave_mapping = _mapping_for_wave(aw.wave, mappings, vm_by_id)
        resolver = _build_resolver(wave_mapping)
        try:
            aw.mtv_yaml = emit_wave_yaml(
                plan_id, aw.wave, aw.description, vm_by_id, resolver, migration_type
            )
        except MTVGenerationError as exc:
            # Surface as the verbatim error; the background task wraps
            # this into the plan.error_message field. We deliberately
            # don't fall back to placeholder YAML — federal customers
            # would rather see a clear failure than apply broken YAML.
            raise PlanValidationError(
                ValidationResult(
                    gaps=[
                        MappingGap(
                            vm_id=0,
                            vm_name="",
                            kind="yaml_emit",
                            source_value=str(exc),
                        )
                    ]
                )
            ) from exc

    return PlanPipelineResult(
        waves=annotated,
        groups=groups,
        method_per_wave={aw.wave.wave_number: aw.method for aw in annotated},
    )
