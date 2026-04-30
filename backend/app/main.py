from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.audit import router as audit_router
from app.api.health import router as health_router
from app.api.plans import router as plans_router
from app.api.settings import settings_router, system_router
from app.api.snapshots import router as snapshots_router
from app.api.templates import router as templates_router
from app.api.vms import router as vms_router
from app.core.db import Base, engine
from app.core.scheduler import shutdown_scheduler, start_scheduler
from app.middleware.audit import AuditMiddleware
from app.models import audit as _audit_models  # noqa: F401  (register models on Base)
from app.models import plan as _plan_models  # noqa: F401  (register models on Base)
from app.models import settings as _settings_models  # noqa: F401  (register models on Base)
from app.models import validation as _validation_models  # noqa: F401  (register models on Base)
from app.models import vm as _vm_models  # noqa: F401  (register models on Base)


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    start_scheduler()
    try:
        yield
    finally:
        shutdown_scheduler()


app = FastAPI(
    title="VirtValidate API",
    description="VM migration validation platform — air-gapped, local LLM",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(AuditMiddleware)

app.include_router(vms_router, prefix="/api/vms")
app.include_router(snapshots_router, prefix="/api/snapshots")
app.include_router(plans_router, prefix="/api/plans")
app.include_router(health_router, prefix="/api/health")
app.include_router(system_router, prefix="/api/system")
app.include_router(settings_router, prefix="/api/settings")
app.include_router(audit_router, prefix="/api/audit")
app.include_router(templates_router, prefix="/api/templates")
