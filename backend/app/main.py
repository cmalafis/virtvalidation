from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.health import router as health_router
from app.api.plans import router as plans_router
from app.api.settings import settings_router, system_router
from app.api.vms import router as vms_router
from app.core.db import Base, engine
from app.core.scheduler import shutdown_scheduler, start_scheduler
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

app.include_router(vms_router)
app.include_router(plans_router)
app.include_router(health_router)
app.include_router(system_router)
app.include_router(settings_router)


@app.get("/health")
def health():
    return {"status": "ok", "version": "0.1.0"}
