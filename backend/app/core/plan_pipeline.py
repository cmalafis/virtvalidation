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
    """

    wave: Wave
    description: str = ""
    risk_score: int = 3
    risk_rationale: str = ""
    notable_concerns: list[str] = field(default_factory=list)
    method: str = "mechanical_fallback"  # llm | llm_retry_N | mechanical_fallback
    mtv_yaml: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Render to the JSON shape persisted in MigrationPlan.waves[]."""
        return {
            "wave_number": self.wave.wave_number,
            "vm_ids": list(self.wave.vm_ids),
            "group_ids": [g.key.as_string() for g in self.wave.groups],
            "vm_count": self.wave.vm_count,
            "estimated_risk": self.wave.estimated_risk,
            "concurrency_group_id": self.wave.concurrency_group_id,
            "description": self.description,
            "risk_score": self.risk_score,
            "risk_rationale": self.risk_rationale,
            "notable_concerns": list(self.notable_concerns),
            "method": self.method,
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


def _vm_payload(vm: VM) -> dict[str, Any]:
    """Shape the MTV emitter expects per VM (a flat dict, not the ORM row)."""
    return {
        "name": vm.name,
        "vsphere_networks": list(vm.vsphere_networks or []),
        "vsphere_datastores": list(vm.vsphere_datastores or []),
        "environment": vm.environment or "",
        "application_hint": vm.application_hint or "",
        "target_namespace": vm.target_namespace or "",
        "target_storage_class": vm.target_storage_class or "",
        "target_network_attachment": vm.target_network_attachment or "",
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


def emit_wave_yaml(
    plan_id: int,
    wave: Wave,
    description: str,
    vm_by_id: dict[int, VM],
    resolver: MappingResolver | None,
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
    )
    return generate_wave_yaml(ctx, vm_payloads, resolver=resolver)


async def run_pipeline(
    vms: list[VM],
    mapping: ResourceMapping | None,
    *,
    plan_id: int,
    backend=None,
    max_llm_attempts: int = 3,
    progress_cb=None,
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
    coverage = validate_plan_inputs(vms, mapping)
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
    _progress("annotating")
    annotated = await annotate_waves(waves, backend=backend, max_attempts=max_llm_attempts)

    # Stage 7 — emit MTV YAML per wave.
    _progress("emitting_yaml")
    vm_by_id = {vm.id: vm for vm in vms}
    resolver = _build_resolver(mapping)
    for aw in annotated:
        try:
            aw.mtv_yaml = emit_wave_yaml(plan_id, aw.wave, aw.description, vm_by_id, resolver)
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
