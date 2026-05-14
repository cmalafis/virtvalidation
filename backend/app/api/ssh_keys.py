"""Multi-key SSH catalog endpoints.

Coexists with the singleton appliance-key endpoints under
``/api/system/ssh-key``. The new flow is wave-scoped — operators
generate per-plan named keys here, then reference them from baseline +
validation runs. The private key NEVER appears in any response.

The ``/{id}/playbook`` endpoint is added in Part 6 alongside the
Ansible playbook file; until that file exists, a 503 is returned.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import PlainTextResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.audit import record_audit
from app.core.db import get_db
from app.core.limits import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE
from app.core.ssh_key import UnsupportedAlgorithmError
from app.core.fips import FIPSViolation
from app.models.ssh_key import SSHKey, SSHKeyStatus
from app.models.vm import VM
from app.schemas.ssh_key import (
    SSHKeyCreate,
    SSHKeyListResponse,
    SSHKeyRead,
    SSHKeyRevocationOutcome,
    SSHKeyRevokeRequest,
    SSHKeyRevokeResponse,
)
from app.services import ssh_key_service

logger = logging.getLogger(__name__)

router = APIRouter(tags=["ssh-keys"])


# Location of the Ansible playbook delivered by GET /{id}/playbook. Lifted
# to a module constant so tests can override it.
_PLAYBOOK_PATH = (
    Path(__file__).resolve().parents[3]
    / "deploy"
    / "ansible"
    / "setup-virtvalidate-user.yml"
)


def _get_or_404(db: Session, key_id: int) -> SSHKey:
    row = db.get(SSHKey, key_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"SSH key {key_id} not found")
    return row


def _actor(request: Request) -> str:
    return request.headers.get("x-actor", "user") if request else "user"


@router.post(
    "",
    response_model=SSHKeyRead,
    status_code=status.HTTP_201_CREATED,
)
def create_ssh_key(
    payload: SSHKeyCreate,
    request: Request,
    db: Session = Depends(get_db),
) -> SSHKeyRead:
    """Generate a new keypair, persist the metadata row, return the row.

    The private key is written to the appliance PVC; only the public key
    and fingerprint are surfaced in the response. The PRIVATE KEY BYTES
    NEVER APPEAR HERE — see ``tests/test_ssh_key_api.py::test_create_does_not_leak_private_key``.
    """
    try:
        row = ssh_key_service.generate_keypair(
            db,
            name=payload.name,
            plan_id=payload.plan_id,
            algorithm=payload.algorithm,
        )
    except UnsupportedAlgorithmError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except FIPSViolation as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    record_audit(
        db,
        action="ssh_key.generated",
        actor=_actor(request),
        resource_type="ssh_key",
        resource_id=row.id,
        details={
            "name": row.name,
            "algorithm": row.algorithm,
            "fingerprint": row.fingerprint,
            "plan_id": row.plan_id,
        },
    )
    db.commit()
    return SSHKeyRead.model_validate(row, from_attributes=True)


@router.get("", response_model=SSHKeyListResponse)
def list_ssh_keys(
    db: Session = Depends(get_db),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    status_filter: SSHKeyStatus | None = Query(default=None, alias="status"),
    plan_id: int | None = Query(default=None),
) -> SSHKeyListResponse:
    """Paginated key catalog. Filterable by status + plan_id."""
    stmt = select(SSHKey)
    count_stmt = select(func.count(SSHKey.id))
    if status_filter is not None:
        stmt = stmt.where(SSHKey.status == status_filter)
        count_stmt = count_stmt.where(SSHKey.status == status_filter)
    if plan_id is not None:
        stmt = stmt.where(SSHKey.plan_id == plan_id)
        count_stmt = count_stmt.where(SSHKey.plan_id == plan_id)

    total = db.execute(count_stmt).scalar_one()
    rows = (
        db.execute(stmt.order_by(SSHKey.id.desc()).offset(skip).limit(limit)).scalars().all()
    )
    return SSHKeyListResponse(
        items=[SSHKeyRead.model_validate(r, from_attributes=True) for r in rows],
        total=total,
        skip=skip,
        limit=limit,
    )


@router.get("/{key_id}", response_model=SSHKeyRead)
def get_ssh_key(key_id: int, db: Session = Depends(get_db)) -> SSHKeyRead:
    row = _get_or_404(db, key_id)
    return SSHKeyRead.model_validate(row, from_attributes=True)


@router.get("/{key_id}/public", response_class=PlainTextResponse)
def get_ssh_key_public(key_id: int, db: Session = Depends(get_db)) -> str:
    """Return the public key as ``text/plain`` for easy copy/paste."""
    row = _get_or_404(db, key_id)
    return row.public_key


@router.get("/{key_id}/playbook")
def get_ssh_key_playbook(key_id: int, db: Session = Depends(get_db)) -> Response:
    """Return the Ansible setup playbook with this key's public material
    templated in, as ``text/yaml``.

    The playbook file is delivered as part of the same repo (see
    ``deploy/ansible/setup-virtvalidate-user.yml``). If the file is
    missing from the deployment, we return 503 so the operator sees a
    deployment-issue surface rather than a confusing 500.
    """
    row = _get_or_404(db, key_id)
    if not _PLAYBOOK_PATH.is_file():
        raise HTTPException(
            status_code=503,
            detail=(
                "Ansible playbook is not present in this deployment. "
                "Expected at deploy/ansible/setup-virtvalidate-user.yml. "
                "Reinstall or copy the file in manually."
            ),
        )
    template = _PLAYBOOK_PATH.read_text(encoding="utf-8")
    # The playbook declares ``virtvalidate_public_key: "REPLACE_ME"`` as a
    # default; substitute the key's public material so operators can run
    # the file directly without ``-e``.
    rendered = template.replace(
        '"REPLACE_ME"',
        f'"{row.public_key.strip()}"',
    )
    headers = {
        "Content-Disposition": (
            f'attachment; filename="virtvalidate-key-{row.id}-setup.yml"'
        )
    }
    return Response(content=rendered, media_type="text/yaml", headers=headers)


@router.post("/{key_id}/retire", response_model=SSHKeyRead)
def retire_ssh_key(
    key_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> SSHKeyRead:
    _get_or_404(db, key_id)
    row = ssh_key_service.retire_key(db, key_id)
    record_audit(
        db,
        action="ssh_key.retired",
        actor=_actor(request),
        resource_type="ssh_key",
        resource_id=row.id,
        details={"name": row.name, "fingerprint": row.fingerprint},
    )
    db.commit()
    return SSHKeyRead.model_validate(row, from_attributes=True)


@router.post("/{key_id}/revoke-from-vms", response_model=SSHKeyRevokeResponse)
def revoke_ssh_key_from_vms(
    key_id: int,
    payload: SSHKeyRevokeRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> SSHKeyRevokeResponse:
    """Day-2 cleanup: remove the key's public component from each listed
    VM's ``~/.ssh/authorized_keys``.

    The wave route ``/api/plans/{id}/waves/{n}/revoke-validation-key``
    delegates here after resolving the wave's VMs. Per-VM failures
    surface in the response but never abort the batch.
    """
    _get_or_404(db, key_id)
    vms = db.execute(select(VM).where(VM.id.in_(payload.vm_ids))).scalars().all()
    found_ids = {vm.id for vm in vms}
    missing = sorted(set(payload.vm_ids) - found_ids)
    if missing:
        raise HTTPException(
            status_code=404,
            detail=f"VMs not found: {missing}",
        )

    targets: list[dict] = []
    for vm in vms:
        host = vm.ip_address or vm.target_hostname or vm.source_hostname
        if not host:
            # No reachable address; record a synthesized failure outcome
            # so the operator sees it in the response.
            targets.append(
                {
                    "vm_id": vm.id,
                    "host": "",
                    "port": vm.ssh_port or 22,
                    "username": vm.ssh_user or "virtvalidate",
                    "_unreachable": True,
                }
            )
            continue
        targets.append(
            {
                "vm_id": vm.id,
                "host": host,
                "port": vm.ssh_port or 22,
                "username": vm.ssh_user or "virtvalidate",
            }
        )

    # Split targets into "real" connections + already-unreachable synthesized
    # failures, so the service only attempts the real ones.
    real = [t for t in targets if not t.get("_unreachable")]
    synthetic_failures = [
        SSHKeyRevocationOutcome(
            vm_id=t["vm_id"],
            succeeded=False,
            detail="no_host_address",
        )
        for t in targets
        if t.get("_unreachable")
    ]
    real_outcomes = ssh_key_service.revoke_key_from_vms(
        db, key_id=key_id, vm_targets=real
    )
    outcomes = synthetic_failures + [
        SSHKeyRevocationOutcome(vm_id=o.vm_id, succeeded=o.succeeded, detail=o.detail)
        for o in real_outcomes
    ]
    succeeded = sum(1 for o in outcomes if o.succeeded)
    failed = sum(1 for o in outcomes if not o.succeeded)

    record_audit(
        db,
        action="ssh_key.revoke_from_vms",
        actor=_actor(request),
        resource_type="ssh_key",
        resource_id=key_id,
        details={
            "total": len(outcomes),
            "succeeded": succeeded,
            "failed": failed,
            "force": payload.force,
        },
    )
    db.commit()
    return SSHKeyRevokeResponse(
        key_id=key_id,
        total=len(outcomes),
        succeeded=succeeded,
        failed=failed,
        outcomes=outcomes,
    )
