from __future__ import annotations

from typing import Any, Callable, Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from keepup_models import HealthResponse, ReadinessResponse


router = APIRouter(tags=["system"])
_get_db: Optional[Callable[[], Any]] = None
_scheduler: Any = None


def configure_system_routes(get_db: Callable[[], Any], scheduler: Any) -> None:
    global _get_db, _scheduler
    _get_db = get_db
    _scheduler = scheduler


@router.get("/health", response_model=HealthResponse)
async def health() -> JSONResponse:
    return JSONResponse({"status": "ok"})


@router.get("/ready", response_model=ReadinessResponse)
async def readiness() -> JSONResponse:
    db_ok = True
    db_error: Optional[str] = None
    try:
        if _get_db is None:
            raise RuntimeError("System router is not configured")
        conn = _get_db()
        try:
            conn.execute("SELECT 1")
        finally:
            conn.close()
    except Exception as exc:
        db_ok = False
        db_error = str(exc)

    scheduler_running = bool(getattr(_scheduler, "running", False))
    try:
        job_count = len(_scheduler.get_jobs()) if _scheduler is not None else 0
    except Exception:
        job_count = 0

    ready = db_ok and scheduler_running
    payload = {
        "ready": ready,
        "db": {"ok": db_ok, "error": db_error},
        "scheduler": {"running": scheduler_running, "jobs": job_count},
    }
    return JSONResponse(payload, status_code=200 if ready else 503)
