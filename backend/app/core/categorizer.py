"""Level 1 categorization — batched LLM classification at scale.

The hierarchical migration planner runs in three levels:

  - **Level 1** (this module) — categorize a vCenter's VMs into
    application / environment / business-unit groupings. Each LLM
    call sees a small batch (default 10 VMs) so the prompt fits
    comfortably under Llama 3 8B's 8192-token context. A 5,000-VM
    vCenter finishes in ~500 calls; tune up the batch on
    larger-context models. See ``docs/LLM_TUNING.md``.

  - **Level 2** (deferred — see ``docs/SCALE.md``) — strategy. One
    call per program, sees group summaries (not individual VMs),
    emits campaign + sequencing structure.

  - **Level 3** (deferred) — wave detail. One call per campaign with
    full VM context, emits MTV-shaped wave assignments.

Today only Level 1 is implemented. The Program / Campaign tables
exist (see ``app/models/grouping.py``) so Level 2/3 can plug in
without a schema break.

Concurrency note: batches are processed **sequentially** today. The
LLMBackend abstraction supports async, but Ollama is single-threaded
and most local-appliance deployments hit it at concurrency=1 anyway.
KServe / vLLM deployments will benefit from parallel batches; that
work is tagged ``parallelize-categorizer`` in the issue queue.
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Literal, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import db as _db_module
from app.core.audit import record_audit
from app.core.config import settings as _module_settings
from app.core.llm.base import LLMBackend, LLMBackendError
from app.core.llm.factory import get_llm_backend
from app.models.grouping import GroupKind, VMGroup, VMGroupMember
from app.models.vcenter import VCenterSource
from app.models.vm import VM

logger = logging.getLogger(__name__)

# Tunables — sized for Ollama llama3:8b on a single-host appliance with
# the default num_ctx=8192. A 10-VM batch fits comfortably with the
# system prompt + JSON response overhead. Larger batches truncate
# silently (the v0.1.x default of 100 was the root cause of the
# "prompt limit=4096 prompt=10420" Ollama warning) and risk
# hallucinated tail entries even when they don't truncate.
#
# Override via the CATEGORIZER_BATCH_SIZE env var when running on a
# larger context model — see docs/LLM_TUNING.md for guidance.
DEFAULT_BATCH_SIZE = _module_settings.categorizer_batch_size
# Hard ceiling. Above this even Llama 3.1's 128K context starts
# producing stale-tail hallucinations because the model loses
# attention on the back half of the list.
MAX_BATCH_SIZE = 200

CategorizationStatus = Literal["running", "completed", "failed"]
CategorizationStep = Literal[
    "queued",
    "loading_inventory",
    "calling_llm",
    "persisting",
    "completed",
    "failed",
]


SYSTEM_PROMPT = """You are VirtValidate, an expert infrastructure architect helping
group a fleet of VMs into logical migration units. You will receive a
batch of VMs from a single vCenter, each described by name, role,
environment, owner, and a free-form application hint.

Your job is to identify, for THIS BATCH only:

  1. Application boundaries — VMs that belong to the same logical
     application based on naming patterns, role, owner, and hints.
     Examples: "epic-emr-prod", "athena-billing", "cerner-imaging".
     Use lowercase kebab-case for application names. Group together
     VMs that share infrastructure even when their roles differ
     (web tier + app tier + db tier of one application = one group).

  2. Environment classification — prod | staging | dev | test | dr.
     Infer from naming convention, environment field, owner. When
     unclear, default to "unclassified".

  3. Business unit groupings — based on owner field, naming patterns
     suggesting departments (e.g. "fin-*" → "finance"), or explicit
     hints. When operator hints are absent, return "unclassified".

A VM SHOULD belong to exactly one group of each kind. A VM MUST NOT
belong to multiple application groups. Confidence (0.0–1.0) reflects
how strongly the evidence supports each assignment — use ≤0.5 when
the only evidence is a single-token name pattern.

Respond with a SINGLE JSON object and nothing else. No markdown
fences. Schema:

{
  "groups": [
    {
      "kind": "application" | "environment" | "business_unit",
      "name": "<lowercase-kebab-case>",
      "description": "<one sentence about what this group represents>",
      "members": [
        {"vm_id": <int>, "confidence": 0.0-1.0, "rationale": "<one short clause>"}
      ]
    }
  ]
}

Every vm_id from the input MUST appear in at least one
"application" group, one "environment" group, and one
"business_unit" group. Do not invent vm_ids that were not provided.
"""


# ---------------------------------------------------------------------------
# Task store — same in-memory + RLock pattern as capture/validation
# ---------------------------------------------------------------------------
@dataclass
class CategorizationTask:
    task_id: str
    source_vcenter_id: int
    status: CategorizationStatus = "running"
    current_step: CategorizationStep = "queued"
    progress_percent: int = 0
    batches_total: int = 0
    batches_complete: int = 0
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: Optional[datetime] = None
    groups_created: Optional[int] = None
    error: Optional[str] = None

    def to_dict(self) -> dict:
        d = asdict(self)
        for key in ("started_at", "completed_at"):
            value = d.get(key)
            if isinstance(value, datetime):
                d[key] = value.isoformat()
        return d


class CategorizationTaskStore:
    def __init__(self) -> None:
        self._tasks: dict[str, CategorizationTask] = {}
        self._lock = threading.RLock()

    def create(self, source_vcenter_id: int) -> CategorizationTask:
        task = CategorizationTask(task_id=str(uuid.uuid4()), source_vcenter_id=source_vcenter_id)
        with self._lock:
            self._tasks[task.task_id] = task
        return task

    def get(self, task_id: str) -> Optional[CategorizationTask]:
        with self._lock:
            return self._tasks.get(task_id)

    def update(self, task_id: str, **fields: Any) -> None:
        with self._lock:
            t = self._tasks.get(task_id)
            if t is None:
                return
            for k, v in fields.items():
                setattr(t, k, v)

    def mark_completed(self, task_id: str, *, groups_created: int) -> None:
        with self._lock:
            t = self._tasks.get(task_id)
            if t is None:
                return
            t.status = "completed"
            t.current_step = "completed"
            t.progress_percent = 100
            t.batches_complete = t.batches_total
            t.groups_created = groups_created
            t.completed_at = datetime.now(timezone.utc)

    def mark_failed(self, task_id: str, *, error: str) -> None:
        with self._lock:
            t = self._tasks.get(task_id)
            if t is None:
                return
            t.status = "failed"
            t.current_step = "failed"
            t.error = error
            t.completed_at = datetime.now(timezone.utc)


task_store = CategorizationTaskStore()


# ---------------------------------------------------------------------------
# Domain logic
# ---------------------------------------------------------------------------
class CategorizationError(RuntimeError):
    """Raised when the categorizer cannot complete a batch."""


def _vm_lite_for_prompt(vm: VM) -> dict:
    """Strip a VM down to the fields the LLM actually needs.

    Sending the full VM record would explode the prompt token budget
    and confuse the model with fields irrelevant to grouping (SSH user,
    target storage class, etc.). 1-2 lines per VM is plenty.
    """
    return {
        "vm_id": vm.id,
        "name": vm.name,
        "role": vm.role or "",
        "environment": vm.environment or "",
        "owner": vm.owner or "",
        "application_hint": vm.application_hint or "",
    }


def _batches(items: list[VM], size: int) -> Iterable[list[VM]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def categorize(
    db: Session,
    *,
    source_vcenter_id: int,
    backend: Optional[LLMBackend] = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    progress_cb: Optional[callable] = None,
) -> dict:
    """Run Level 1 categorization for one vCenter.

    Replaces any pre-existing groups for this vCenter — categorization
    is idempotent and re-running on the same scope produces a fresh
    set. Operators who want to compare runs export the previous
    grouping first (audit log carries the run_id).

    Returns a summary dict suitable for surfacing to the UI:
      ``{"groups_created": int, "batches_processed": int}``
    """
    backend = backend or get_llm_backend()
    batch_size = max(1, min(batch_size, MAX_BATCH_SIZE))

    # Validate the vCenter exists before doing any work — saves a
    # confusing "no VMs to categorize" return from a typo'd id.
    vcenter = db.get(VCenterSource, source_vcenter_id)
    if vcenter is None:
        raise CategorizationError(f"vCenter source {source_vcenter_id} not found")

    vms = list(db.scalars(select(VM).where(VM.source_vcenter_id == source_vcenter_id)).all())
    if not vms:
        # Empty inventory — return an empty result rather than crash.
        # The UI surfaces "No VMs to categorize" and prompts the
        # operator to upload an RVTools file first.
        return {"groups_created": 0, "batches_processed": 0}

    # Drop existing groups for a clean re-run. Cascade deletes wipe
    # member associations too. We keep the same vCenter so any audit
    # trail referencing the run_id stays intact.
    db.query(VMGroupMember).filter(
        VMGroupMember.group_id.in_(
            select(VMGroup.id).where(VMGroup.source_vcenter_id == source_vcenter_id)
        )
    ).delete(synchronize_session=False)
    db.query(VMGroup).filter(VMGroup.source_vcenter_id == source_vcenter_id).delete(
        synchronize_session=False
    )
    db.flush()

    run_id = str(uuid.uuid4())
    aggregated: dict[tuple[GroupKind, str], dict] = {}  # (kind, name) → group payload
    members_buffer: list[tuple[tuple[GroupKind, str], int, float, str]] = []

    batches = list(_batches(vms, batch_size))
    if progress_cb is not None:
        progress_cb(batches_total=len(batches), batches_complete=0)

    for batch_index, batch in enumerate(batches, start=1):
        try:
            response = _call_batch(backend, batch)
        except LLMBackendError as e:
            raise CategorizationError(
                f"LLM backend failure on batch {batch_index}/{len(batches)}: {e}"
            ) from e
        try:
            parsed = _parse_batch_response(response.get("content", ""))
        except CategorizationError as e:
            # Surface the batch index so the operator can correlate
            # with the LLM logs if they're chasing a specific failure.
            raise CategorizationError(
                f"Batch {batch_index}/{len(batches)} parse failure: {e}"
            ) from e

        for group in parsed.get("groups") or []:
            kind = _coerce_kind(group.get("kind"))
            if kind is None:
                continue
            name = (group.get("name") or "").strip().lower()
            if not name:
                continue
            key = (kind, name)
            agg = aggregated.setdefault(
                key,
                {
                    "kind": kind,
                    "name": name,
                    "description": (group.get("description") or "").strip(),
                },
            )
            # Take the longer description across batches — first batch
            # to describe a group sets the floor; later batches replace
            # it only when they say more.
            new_desc = (group.get("description") or "").strip()
            if len(new_desc) > len(agg.get("description") or ""):
                agg["description"] = new_desc
            for m in group.get("members") or []:
                vm_id = m.get("vm_id")
                if not isinstance(vm_id, int):
                    continue
                conf_raw = m.get("confidence", 1.0)
                try:
                    conf = max(0.0, min(1.0, float(conf_raw)))
                except (TypeError, ValueError):
                    conf = 1.0
                rationale = (m.get("rationale") or "").strip()
                members_buffer.append((key, vm_id, conf, rationale))

        if progress_cb is not None:
            progress_cb(batches_total=len(batches), batches_complete=batch_index)

    # Persist groups + members in one transactional pass. The unique
    # constraint on VMGroup(vcenter, kind, name) is the safety net for
    # the (unlikely) case where the LLM emits two groups with the same
    # name+kind in the same run.
    valid_vm_ids = {vm.id for vm in vms}
    group_objs: dict[tuple[GroupKind, str], VMGroup] = {}
    for key, payload in aggregated.items():
        group = VMGroup(
            source_vcenter_id=source_vcenter_id,
            kind=payload["kind"],
            name=payload["name"],
            description=payload["description"] or None,
            created_by_run_id=run_id,
        )
        db.add(group)
        group_objs[key] = group
    db.flush()

    seen_membership: set[tuple[int, int]] = set()  # (group_id, vm_id) dedup
    members_added = 0
    for key, vm_id, confidence, rationale in members_buffer:
        if vm_id not in valid_vm_ids:
            # LLM hallucinated a VM id — drop silently. Log so we can
            # spot recurring issues without blowing up the run.
            logger.warning(
                "Categorizer dropped hallucinated vm_id=%s for group %s/%s",
                vm_id,
                key[0].value,
                key[1],
            )
            continue
        group = group_objs.get(key)
        if group is None:
            continue
        member_key = (group.id, vm_id)
        if member_key in seen_membership:
            continue
        seen_membership.add(member_key)
        db.add(
            VMGroupMember(
                group_id=group.id,
                vm_id=vm_id,
                confidence=confidence,
                rationale=rationale or None,
            )
        )
        members_added += 1

    record_audit(
        db,
        action="categorization.completed",
        actor="user",
        resource_type="vcenter",
        resource_id=source_vcenter_id,
        details={
            "run_id": run_id,
            "vm_count": len(vms),
            "batches": len(batches),
            "groups_created": len(group_objs),
            "members_added": members_added,
        },
    )
    db.commit()

    return {
        "groups_created": len(group_objs),
        "batches_processed": len(batches),
        "members_added": members_added,
        "run_id": run_id,
    }


def _coerce_kind(raw: Any) -> Optional[GroupKind]:
    if not raw:
        return None
    try:
        return GroupKind(str(raw).lower())
    except ValueError:
        return None


def _call_batch(backend: LLMBackend, batch: list[VM]) -> dict:
    payload = {
        "vcenter": batch[0].source_vcenter_id if batch else None,
        "batch_size": len(batch),
        "vms": [_vm_lite_for_prompt(vm) for vm in batch],
    }
    return backend.chat_sync(
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    "Categorize the following VMs and return the JSON object "
                    "specified in the system prompt:\n\n"
                    + json.dumps(payload, indent=2, sort_keys=True)
                ),
            },
        ],
        temperature=0.1,
    )


def _parse_batch_response(raw: str) -> dict:
    if not raw:
        raise CategorizationError("LLM returned empty content")
    try:
        body = json.loads(raw)
    except json.JSONDecodeError as e:
        raise CategorizationError(f"LLM output not valid JSON: {e}") from e
    if not isinstance(body, dict):
        raise CategorizationError("LLM output not a JSON object")
    groups = body.get("groups")
    if not isinstance(groups, list):
        raise CategorizationError("LLM output missing 'groups' list")
    return body


# ---------------------------------------------------------------------------
# BackgroundTask entry point
# ---------------------------------------------------------------------------
def run_categorization_task(
    task_id: str, source_vcenter_id: int, *, batch_size: int = DEFAULT_BATCH_SIZE
) -> None:
    """Body of the FastAPI BackgroundTask that drives a categorization run.

    Opens its own DB session because BackgroundTasks fire after the
    request handler has returned. Same pattern as the capture / validation
    background workflows.
    """
    db = _db_module.SessionLocal()
    try:
        task_store.update(task_id, current_step="loading_inventory", progress_percent=5)

        def _progress(*, batches_total: int, batches_complete: int) -> None:
            task_store.update(
                task_id,
                current_step="calling_llm",
                batches_total=batches_total,
                batches_complete=batches_complete,
                # Reserve the last 10% for persistence; the LLM phase
                # owns the 5–90% band so the bar moves while the work
                # is actually happening.
                progress_percent=int(5 + (85 * batches_complete / max(batches_total, 1))),
            )

        try:
            result = categorize(
                db,
                source_vcenter_id=source_vcenter_id,
                progress_cb=_progress,
                batch_size=batch_size,
            )
        except CategorizationError as e:
            logger.warning("categorization failed for vcenter %s: %s", source_vcenter_id, e)
            task_store.mark_failed(task_id, error=str(e))
            return

        task_store.update(task_id, current_step="persisting", progress_percent=95)
        task_store.mark_completed(task_id, groups_created=result["groups_created"])
        logger.info(
            "categorization task %s completed: %d groups across %d batches",
            task_id,
            result["groups_created"],
            result["batches_processed"],
        )
    finally:
        db.close()
