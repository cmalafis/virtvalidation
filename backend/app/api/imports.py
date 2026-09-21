"""Server-side inventory import: upload → scan → route → import.

The browser uploads the file and polls; it never parses. See
``app.core.import_jobs`` for the runner and ``app.core.rvtools_parser`` for
the streaming parser.

    POST /api/imports/rvtools            multipart upload → 202 + job
    GET  /api/imports/{id}               poll
    POST /api/imports/{id}/start         supply vCenter routing → importing
    POST /api/imports/{id}/cancel
    GET  /api/imports/{id}/rejects       paginated problem rows
    GET  /api/imports/{id}/rejects.csv   the same, downloadable
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import uuid
from pathlib import Path

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.import_jobs import run_import, run_scan, spool_dir
from app.core.limits import DEFAULT_PAGE_SIZE, MAX_IMPORT_UPLOAD_BYTES, MAX_PAGE_SIZE
from app.core.rvtools_parser import normalize_hostname
from app.models.import_job import (
    IMPORT_ACTIVE_STATUSES,
    IMPORT_TERMINAL_STATUSES,
    ImportJob,
    ImportJobReject,
)
from app.models.vcenter import VCenterSource
from app.schemas.import_job import (
    ImportJobListResponse,
    ImportJobRead,
    ImportRejectListResponse,
    ImportStartRequest,
)

router = APIRouter(tags=["imports"])

_ALLOWED_SUFFIXES = (".xlsx", ".csv")
_CHUNK = 1024 * 1024


def _get_job(db: Session, job_id: str) -> ImportJob:
    job = db.get(ImportJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Import job not found")
    return job


def _validated_mapping(
    db: Session, mapping: dict[str, int], default_vcenter_id: int | None
) -> dict[str, int]:
    normalized = {normalize_hostname(k) or "": v for k, v in mapping.items()}
    wanted = set(normalized.values()) | ({default_vcenter_id} - {None})
    known = set(db.scalars(select(VCenterSource.id).where(VCenterSource.id.in_(wanted))).all())
    if wanted - known:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Routing references unknown vCenter source id(s): {sorted(wanted - known)}. "
                "Register them first or remove them from the routing."
            ),
        )
    return normalized


def _refuse_if_another_is_running(db: Session) -> None:
    # Two imports racing into the same vCenter would each miss the other's
    # uncommitted rows and both "create" the same VM.
    busy = db.scalar(select(ImportJob.id).where(ImportJob.status.in_(IMPORT_ACTIVE_STATUSES)))
    if busy:
        raise HTTPException(
            status_code=409,
            detail=f"Another import ({busy}) is still running. Wait for it or cancel it.",
        )


@router.post("/rvtools", response_model=ImportJobRead, status_code=202)
def upload_rvtools(
    request: Request,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    mode: str = Form("upsert"),
    vcenter_mapping: str | None = Form(None, description="JSON object: hostname → vCenter id"),
    default_vcenter_id: int | None = Form(None),
    db: Session = Depends(get_db),
) -> ImportJob:
    if mode not in ("create_only", "upsert"):
        raise HTTPException(status_code=422, detail="mode must be 'create_only' or 'upsert'")
    filename = Path(file.filename or "upload").name[:255]
    suffix = Path(filename).suffix.lower()
    if suffix == ".xls":
        raise HTTPException(
            status_code=415,
            detail="Legacy .xls is not supported. Re-save the RVTools export as .xlsx.",
        )
    if suffix not in _ALLOWED_SUFFIXES:
        raise HTTPException(status_code=415, detail="Upload an RVTools .xlsx export or a .csv")

    mapping: dict[str, int] = {}
    if vcenter_mapping:
        try:
            raw = json.loads(vcenter_mapping)
            mapping = {str(k): int(v) for k, v in raw.items()}
        except (ValueError, AttributeError, TypeError) as e:
            raise HTTPException(
                status_code=422, detail="vcenter_mapping must be a JSON object of hostname → id"
            ) from e
    mapping = _validated_mapping(db, mapping, default_vcenter_id)
    _refuse_if_another_is_running(db)

    job_id = str(uuid.uuid4())
    dest = spool_dir() / f"{job_id}{suffix}"
    digest = hashlib.sha256()
    size = 0
    try:
        with dest.open("wb") as out:
            while chunk := file.file.read(_CHUNK):
                size += len(chunk)
                if size > MAX_IMPORT_UPLOAD_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail=(
                            f"File exceeds {MAX_IMPORT_UPLOAD_BYTES // (1024 * 1024)} MB "
                            "(MAX_IMPORT_UPLOAD_BYTES)."
                        ),
                    )
                digest.update(chunk)
                out.write(chunk)
        if size == 0:
            raise HTTPException(status_code=422, detail="The uploaded file is empty")
    except HTTPException:
        dest.unlink(missing_ok=True)
        raise

    job = ImportJob(
        id=job_id,
        kind="rvtools",
        filename=filename,
        file_path=str(dest),
        file_size=size,
        file_sha256=digest.hexdigest(),
        mode=mode,
        status="uploaded",
        progress_message="Queued",
        vcenter_mapping=mapping,
        default_vcenter_id=default_vcenter_id,
        actor=request.headers.get("x-actor", "user"),
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    background_tasks.add_task(run_scan, job_id)
    return job


@router.get("", response_model=ImportJobListResponse)
def list_imports(
    skip: int = Query(0, ge=0),
    limit: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    db: Session = Depends(get_db),
) -> dict:
    total = db.scalar(select(func.count()).select_from(ImportJob)) or 0
    items = db.scalars(
        select(ImportJob).order_by(ImportJob.created_at.desc()).offset(skip).limit(limit)
    ).all()
    return {"items": list(items), "total": total, "skip": skip, "limit": limit}


@router.get("/{job_id}", response_model=ImportJobRead)
def get_import(job_id: str, db: Session = Depends(get_db)) -> ImportJob:
    return _get_job(db, job_id)


@router.post("/{job_id}/start", response_model=ImportJobRead, status_code=202)
def start_import(
    job_id: str,
    payload: ImportStartRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> ImportJob:
    job = _get_job(db, job_id)
    if job.status != "awaiting_mapping":
        raise HTTPException(
            status_code=409,
            detail=f"Import is '{job.status}'; routing can only be supplied while it is awaiting_mapping",
        )
    if not payload.vcenter_mapping and payload.default_vcenter_id is None:
        raise HTTPException(
            status_code=422,
            detail="Route at least one detected vCenter hostname, or pick a default vCenter",
        )
    job.vcenter_mapping = _validated_mapping(
        db, payload.vcenter_mapping, payload.default_vcenter_id
    )
    job.default_vcenter_id = payload.default_vcenter_id
    if payload.mode:
        job.mode = payload.mode
    _refuse_if_another_is_running(db)
    job.status = "importing"
    job.progress_message = "Queued"
    db.commit()
    db.refresh(job)
    background_tasks.add_task(run_import, job_id)
    return job


@router.post("/{job_id}/cancel", response_model=ImportJobRead)
def cancel_import(job_id: str, db: Session = Depends(get_db)) -> ImportJob:
    job = _get_job(db, job_id)
    if job.status in IMPORT_TERMINAL_STATUSES:
        return job
    if job.status == "awaiting_mapping":
        # Nothing is running — finish it here.
        from app.core.import_jobs import _finish

        job.progress_message = "Cancelled before import started"
        _finish(db, job, "cancelled")
    else:
        # The runner checks this flag between batches.
        job.cancel_requested = True
        db.commit()
    db.refresh(job)
    return job


def _rejects_query(job_id: str, severity: str | None):
    q = select(ImportJobReject).where(ImportJobReject.job_id == job_id)
    if severity:
        q = q.where(ImportJobReject.severity == severity)
    return q


@router.get("/{job_id}/rejects", response_model=ImportRejectListResponse)
def list_rejects(
    job_id: str,
    severity: str | None = Query(None, pattern="^(rejected|warning)$"),
    skip: int = Query(0, ge=0),
    limit: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    db: Session = Depends(get_db),
) -> dict:
    _get_job(db, job_id)
    base = _rejects_query(job_id, severity)
    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    items = db.scalars(
        base.order_by(ImportJobReject.sheet, ImportJobReject.row_number).offset(skip).limit(limit)
    ).all()
    return {"items": list(items), "total": total, "skip": skip, "limit": limit}


def _csv_safe(value: str | None) -> str:
    # A VM named "=HYPERLINK(...)" must not execute when the operator
    # opens the report in Excel.
    text = value or ""
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


@router.get("/{job_id}/rejects.csv")
def download_rejects(job_id: str, db: Session = Depends(get_db)) -> StreamingResponse:
    job = _get_job(db, job_id)
    rows = db.scalars(
        _rejects_query(job_id, None).order_by(ImportJobReject.sheet, ImportJobReject.row_number)
    ).all()
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["sheet", "row", "vm_name", "severity", "reason"])
    for r in rows:
        writer.writerow(
            [r.sheet, r.row_number, _csv_safe(r.vm_name), r.severity, _csv_safe(r.reason)]
        )
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="import-{job.id[:8]}-rejects.csv"'},
    )
