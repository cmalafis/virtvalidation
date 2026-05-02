import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.audit import router as audit_router
from app.api.health import router as health_router
from app.api.network_reviews import router as network_reviews_router
from app.api.plans import router as plans_router
from app.api.reports import router as reports_router
from app.api.settings import settings_router, system_router
from app.api.snapshots import router as snapshots_router
from app.api.templates import router as templates_router
from app.api.validations import router as validations_router
from app.api.vcenters import router as vcenters_router
from app.api.vms import router as vms_router
from app.core.db import engine
from app.core.fips import log_startup_warning as _fips_startup_log
from app.core.migrations import MigrationError, apply_migrations
from app.core.scheduler import shutdown_scheduler, start_scheduler
from app.middleware.audit import AuditMiddleware
from app.models import audit as _audit_models  # noqa: F401  (register models on Base)
from app.models import grouping as _grouping_models  # noqa: F401  (register models on Base)
from app.models import plan as _plan_models  # noqa: F401  (register models on Base)
from app.models import settings as _settings_models  # noqa: F401  (register models on Base)
from app.models import validation as _validation_models  # noqa: F401  (register models on Base)
from app.models import vcenter as _vcenter_models  # noqa: F401  (register models on Base)
from app.models import vm as _vm_models  # noqa: F401  (register models on Base)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Apply database migrations BEFORE serving any traffic. We previously
    # ran ``Base.metadata.create_all`` which only creates missing tables
    # — column additions were silently dropped, leading to confused 500s
    # at runtime. Now every schema change rides on Alembic; the bridge
    # logic in apply_migrations handles legacy v0.1.x deployments by
    # stamping head before the upgrade runs.
    #
    # Fail-fast on any migration error: a half-migrated DB is worse than
    # not starting at all. ``SystemExit(1)`` triggers a container restart
    # which surfaces the failure loudly in pod logs / podman ps.
    try:
        apply_migrations(engine)
    except MigrationError as e:
        logger.error("Migration failed: %s", e)
        raise SystemExit(1) from e

    # Log the FIPS posture at boot so federal deployments leave a clear
    # breadcrumb in container logs about whether the application is
    # actually running in compliance mode.
    _fips_startup_log()
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
app.include_router(reports_router, prefix="/api/reports")
app.include_router(network_reviews_router, prefix="/api/network-reviews")
app.include_router(validations_router, prefix="/api/validations")
app.include_router(vcenters_router, prefix="/api/sources/vcenters")
