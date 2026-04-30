"""Static template downloads — currently just the bulk inventory CSV."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from app.core.config import settings

router = APIRouter(tags=["templates"])

_CSV_FILENAME = "vm-inventory-template.csv"


@router.get("/csv")
def download_csv_template() -> Response:
    """Serve the canonical VM inventory CSV template as a download.

    The file is shipped inside the container at the path configured by
    ``CSV_TEMPLATE_PATH`` (default ``/app/templates/vm-inventory-template.csv``).
    Operators running the backend outside a container can point the env var
    at the repo's ``docs/vm-inventory-template.csv``.
    """
    path = Path(settings.csv_template_path)
    if not path.is_file():
        raise HTTPException(
            status_code=500,
            detail=(
                f"CSV template not found at {path}. Set CSV_TEMPLATE_PATH "
                "or rebuild the container so the template is copied into the image."
            ),
        )

    body = path.read_bytes()
    return Response(
        content=body,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{_CSV_FILENAME}"'},
    )
