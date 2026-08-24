"""Orchestration subsystem.

Exposes the pipeline engine, stage primitives, declarative pipeline model,
and the cron scheduler in one convenient namespace::

    from src.orchestrator import PipelineEngine, Pipeline, CommandStage
"""

from __future__ import annotations

from src.orchestrator.engine import PipelineEngine, PipelineRun
from src.orchestrator.pipeline import (
    CommandStage,
    Pipeline,
    PipelineValidationError,
    Trigger,
)
from src.orchestrator.scheduler import CronTrigger, Scheduler, ScheduleEntry, TriggerManager
from src.orchestrator.stage import (
    BaseStage,
    ExecutionContext,
    ParallelGroup,
    RetryPolicy,
    SequentialGroup,
    StageResult,
    StageStatus,
)

__all__ = [
    "BaseStage",
    "CommandStage",
    "CronTrigger",
    "ExecutionContext",
    "ParallelGroup",
    "Pipeline",
    "PipelineEngine",
    "PipelineRun",
    "PipelineValidationError",
    "RetryPolicy",
    "ScheduleEntry",
    "Scheduler",
    "SequentialGroup",
    "StageResult",
    "StageStatus",
    "Trigger",
    "TriggerManager",
]
