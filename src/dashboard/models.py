"""Pydantic models for the dashboard REST API."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


def utcnow() -> datetime:
    """Current UTC timestamp used for model defaults."""
    return datetime.now(timezone.utc)


class StageRunModel(BaseModel):
    """One stage's outcome inside a run."""

    model_config = ConfigDict(extra="ignore")

    name: str
    status: str = "PENDING"
    exit_code: int = -1
    duration: float = 0.0
    attempts: int = 1
    output: str = ""
    error: str = ""
    artifacts: list[str] = Field(default_factory=list)


class RunSummary(BaseModel):
    """Aggregated counters for a pipeline run."""

    total: int = 0
    success: int = 0
    failed: int = 0
    skipped: int = 0


class RunModel(BaseModel):
    """A pipeline run exposed over the API."""

    model_config = ConfigDict(extra="ignore", from_attributes=True)

    id: str
    pipeline: str
    trigger: str = "manual"
    status: str = "PENDING"
    started_at: datetime = Field(default_factory=utcnow)
    finished_at: Optional[datetime] = None
    duration: float = 0.0
    event: Optional[dict[str, Any]] = None
    results: dict[str, StageRunModel] = Field(default_factory=dict)

    @property
    def summary(self) -> RunSummary:
        """Count stages by coarse outcome buckets."""
        counts = RunSummary(total=len(self.results))
        for result in self.results.values():
            if result.status == "SUCCESS":
                counts.success += 1
            elif result.status in {"FAILED", "CANCELLED"}:
                counts.failed += 1
            elif result.status == "SKIPPED":
                counts.skipped += 1
        return counts


class TriggerModel(BaseModel):
    """Trigger definition as accepted by the API."""

    kind: str = "manual"
    branches: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    events: list[str] = Field(default_factory=list)
    schedule: Optional[str] = None


class PipelineModel(BaseModel):
    """A registered pipeline definition."""

    model_config = ConfigDict(extra="ignore")

    name: str = Field(min_length=1, max_length=120)
    description: str = ""
    stages: list[str] = Field(default_factory=list)
    triggers: list[TriggerModel] = Field(default_factory=list)
    environment: dict[str, str] = Field(default_factory=dict)
    max_parallel: int = Field(default=4, ge=1, le=64)


class CreatePipelineRequest(BaseModel):
    """Body for POST /pipelines."""

    name: str = Field(min_length=1, max_length=120)
    description: str = ""
    environment: dict[str, str] = Field(default_factory=dict)


class CreateRunRequest(BaseModel):
    """Body for POST /pipelines/{name}/run."""

    trigger: str = "manual"
    environment: dict[str, str] = Field(default_factory=dict)
    variables: dict[str, Any] = Field(default_factory=dict)


class LogLine(BaseModel):
    """One line of captured stage output."""

    timestamp: datetime = Field(default_factory=utcnow)
    stream: str = "stdout"
    stage: str = ""
    message: str = ""


class HealthResponse(BaseModel):
    """Liveness payload returned by /health."""

    status: str = "ok"
    version: str = "1.0.0"
    uptime_seconds: float = 0.0
    pipelines: int = 0
    runs: int = 0


class WebhookAck(BaseModel):
    """Standard response for webhook ingestion endpoints."""

    source: str
    handled: bool = False
    responses: list[dict[str, Any]] = Field(default_factory=list)
