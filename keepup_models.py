from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    status: str = "ok"


class DatabaseReadiness(BaseModel):
    ok: bool
    error: Optional[str] = None


class SchedulerReadiness(BaseModel):
    running: bool
    jobs: int = Field(ge=0)


class ReadinessResponse(BaseModel):
    ready: bool
    db: DatabaseReadiness
    scheduler: SchedulerReadiness
