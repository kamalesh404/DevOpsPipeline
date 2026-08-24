"""Web dashboard and REST API."""

from __future__ import annotations

from src.dashboard.app import Registry, create_app, create_default_app
from src.dashboard.models import (
    CreatePipelineRequest,
    CreateRunRequest,
    HealthResponse,
    LogLine,
    PipelineModel,
    RunModel,
    StageRunModel,
)

__all__ = [
    "CreatePipelineRequest",
    "CreateRunRequest",
    "HealthResponse",
    "LogLine",
    "PipelineModel",
    "Registry",
    "RunModel",
    "StageRunModel",
    "create_app",
    "create_default_app",
]
