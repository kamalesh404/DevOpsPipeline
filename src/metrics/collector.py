"""Prometheus metrics collection for pipeline activity.

Wraps prometheus_client counters, histograms and gauges in a small facade so
the rest of the codebase never touches metric internals. Each collector owns
its own :class:`CollectorRegistry`, which keeps tests hermetic and allows
multiple isolated collectors in one process.
"""

from __future__ import annotations

import threading
from typing import Optional

from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Gauge, Histogram, generate_latest

METRIC_PREFIX = "devops"
STAGE_BUCKETS = (0.5, 1, 2.5, 5, 10, 30, 60, 120, 300, 600, 1800)


class MetricsCollector:
    """Facade over the Prometheus client used across DevOpsPipeline."""

    def __init__(self, registry: Optional[CollectorRegistry] = None) -> None:
        self.registry = registry or CollectorRegistry(auto_describe=True)
        self._runs_total = Counter(
            f"{METRIC_PREFIX}_pipeline_runs_total",
            "Total number of pipeline runs by terminal status",
            labelnames=("pipeline", "status"),
            registry=self.registry,
        )
        self._stage_total = Counter(
            f"{METRIC_PREFIX}_stage_runs_total",
            "Total number of stage executions by status",
            labelnames=("pipeline", "stage", "status"),
            registry=self.registry,
        )
        self._stage_duration = Histogram(
            f"{METRIC_PREFIX}_stage_duration_seconds",
            "Stage wall-clock duration in seconds",
            labelnames=("pipeline", "stage"),
            buckets=STAGE_BUCKETS,
            registry=self.registry,
        )
        self._active_runs = Gauge(
            f"{METRIC_PREFIX}_active_runs",
            "Currently executing pipeline runs",
            registry=self.registry,
        )
        self._queue_depth = Gauge(
            f"{METRIC_PREFIX}_queue_depth",
            "Runs waiting to be picked up",
            registry=self.registry,
        )
        self._lock = threading.Lock()

    # -- recording ----------------------------------------------------------
    def record_pipeline_run(self, pipeline: str, status: str) -> None:
        """Count a finished run under its terminal status."""
        self._runs_total.labels(pipeline=pipeline, status=status).inc()

    def record_stage_run(self, pipeline: str, stage: str, status: str) -> None:
        """Count one stage execution outcome."""
        self._stage_total.labels(pipeline=pipeline, stage=stage, status=status).inc()

    def observe_stage_duration(self, pipeline: str, stage: str, duration_seconds: float) -> None:
        """Record how long a stage took."""
        self._stage_duration.labels(pipeline=pipeline, stage=stage).observe(max(0.0, duration_seconds))

    def record_stage(self, pipeline: str, stage_result: object) -> None:
        """Convenience: record both status counter and duration histogram.

        ``stage_result`` is any object exposing ``name``, ``status`` (a
        StageStatus-like enum) and ``duration``.
        """
        status_value = getattr(getattr(stage_result, "status", None), "value", "UNKNOWN")
        self.record_stage_run(pipeline, str(getattr(stage_result, "name", "?")), str(status_value))
        self.observe_stage_duration(pipeline, str(getattr(stage_result, "name", "?")), float(getattr(stage_result, "duration", 0.0)))

    def record_pipeline(self, pipeline: str, run: object) -> None:
        """Record a completed PipelineRun-like object."""
        status_value = str(getattr(run, "status", None)) if not hasattr(getattr(run, "status", None), "value") else getattr(run.status, "value")
        self.record_pipeline_run(pipeline, status_value)

    # -- gauges ---------------------------------------------------------------
    def inc_active_runs(self) -> None:
        """Mark one more run as in-flight."""
        with self._lock:
            self._active_runs.inc()

    def dec_active_runs(self) -> None:
        """Mark one run as finished; never goes below zero."""
        with self._lock:
            value = self._active_runs._value.get()  # noqa: SLF001 - guarded decrement
            if value > 0:
                self._active_runs.dec()

    def set_queue_depth(self, depth: int) -> None:
        """Publish the current scheduler queue depth."""
        self._queue_depth.set(max(0, depth))

    # -- export -------------------------------------------------------------
    def export(self) -> tuple[bytes, str]:
        """Render the exposition format along with its content type."""
        return generate_latest(self.registry), CONTENT_TYPE_LATEST


_default_collector: Optional[MetricsCollector] = None
_default_lock = threading.Lock()


def get_collector() -> MetricsCollector:
    """Process-wide shared collector (created lazily)."""
    global _default_collector
    with _default_lock:
        if _default_collector is None:
            _default_collector = MetricsCollector()
        return _default_collector
