"""Persist one :class:`~app.models.inference_log.InferenceLog` per LLM call.

Called from the orchestrator layer (validation, wave annotation) where a DB
session is in scope. Mirrors the defensive contract of
``app.core.validation._record_llm_usage``: capture is best-effort and MUST
never mask the LLM result — a failure to write the audit row is logged and
swallowed, the caller's verdict still returns.

Backends stay pure transport and never import this; the input messages and
raw output are threaded out of the backend call by the orchestrator (the
validation client via a ``capture`` out-dict, wave annotation via fields on
``AnnotatedWave``).
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# Cap on stored raw response / serialized message size, guarding against a
# pathological model dumping megabytes into one audit row. Generous enough
# that a normal validation prompt + response fits whole.
_MAX_OUTPUT_CHARS = 64_000


def _truncate(text: str | None) -> str | None:
    if text is None:
        return None
    if len(text) <= _MAX_OUTPUT_CHARS:
        return text
    return text[:_MAX_OUTPUT_CHARS] + f"\n…[truncated {len(text) - _MAX_OUTPUT_CHARS} chars]"


def record_inference(
    db: Session,
    *,
    operation: str,
    backend_type: str,
    model: str,
    method: str = "llm",
    input_messages: list | None = None,
    output_text: str | None = None,
    detections: dict | None = None,
    verdict: str | None = None,
    latency_ms: int = 0,
    resource_type: str | None = None,
    resource_id: int | None = None,
    vm_id: int | None = None,
) -> None:
    """Write one InferenceLog row. Best-effort — never raises to the caller."""
    from app.models.inference_log import InferenceLog  # local: registers on Base

    try:
        db.add(
            InferenceLog(
                operation=operation,
                backend_type=backend_type or "unknown",
                model=model or "",
                method=method or "llm",
                input_messages=input_messages,
                output_text=_truncate(output_text),
                detections=detections,
                verdict=verdict,
                latency_ms=int(latency_ms or 0),
                resource_type=resource_type,
                resource_id=resource_id,
                vm_id=vm_id,
            )
        )
        db.commit()
    except Exception:  # noqa: BLE001 — audit capture must not break the flow
        logger.exception("inference_log.record_failed operation=%s vm_id=%s", operation, vm_id)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
