"""Cron scheduling and trigger management.

The :class:`Scheduler` fires pipeline callbacks on cron expressions, while the
:class:`TriggerManager` maps incoming webhook/push events onto registered
pipelines by evaluating their :class:`~src.orchestrator.pipeline.Trigger`
rules. Running this module directly starts a demo scheduler loop.
"""

from __future__ import annotations

import logging
import threading
import time
from croniter import croniter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from src.orchestrator.pipeline import Pipeline

LOG = logging.getLogger("devopspipeline.scheduler")

PipelineCallback = Callable[[str], None]


@dataclass
class CronTrigger:
    """A validated 5-field cron expression wrapper."""

    expression: str

    def __post_init__(self) -> None:
        if not croniter.is_valid(self.expression):
            raise ValueError(f"invalid cron expression: {self.expression!r}")

    def next_after(self, moment: datetime) -> datetime:
        """Return the next fire time strictly after ``moment`` (UTC aware)."""
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return croniter(self.expression, moment).get_next(datetime)

    def describe(self) -> str:
        """Human-readable description used by listings."""
        return f"cron({self.expression})"


@dataclass
class ScheduleEntry:
    """One scheduled pipeline registration inside a Scheduler."""

    name: str
    pipeline_name: str
    trigger: CronTrigger
    callback: PipelineCallback | None = None
    enabled: bool = True
    last_run: datetime | None = None
    next_run: datetime | None = field(default=None)

    def compute_next_run(self, now: datetime) -> datetime:
        """Recompute and cache the next fire time from ``now``."""
        self.next_run = self.trigger.next_after(now)
        return self.next_run


class Scheduler:
    """Registry + dispatcher for cron-scheduled pipelines."""

    def __init__(self) -> None:
        self._entries: dict[str, ScheduleEntry] = {}
        self._lock = threading.RLock()

    def add_entry(
        self,
        entry: ScheduleEntry,
        *,
        now: datetime | None = None,
    ) -> ScheduleEntry:
        """Register an entry and seed its initial next-run timestamp."""
        with self._lock:
            if entry.name in self._entries:
                raise ValueError(f"schedule '{entry.name}' already exists")
            entry.compute_next_run(now or datetime.now(timezone.utc))
            self._entries[entry.name] = entry
            return entry

    def schedule_pipeline(
        self,
        pipeline_name: str,
        expression: str,
        callback: PipelineCallback | None = None,
        *,
        name: str | None = None,
    ) -> ScheduleEntry:
        """Convenience constructor+registration for one pipeline."""
        entry = ScheduleEntry(
            name=name or f"{pipeline_name}:{expression}",
            pipeline_name=pipeline_name,
            trigger=CronTrigger(expression),
            callback=callback,
        )
        return self.add_entry(entry)

    def remove_entry(self, name: str) -> bool:
        """Unregister an entry; returns False when it did not exist."""
        with self._lock:
            return self._entries.pop(name, None) is not None

    def pause(self, name: str) -> None:
        """Temporarily disable an entry without deleting it."""
        with self._lock:
            self._entries[name].enabled = False

    def resume(self, name: str) -> None:
        """Re-enable a paused entry and refresh its next run."""
        now = datetime.now(timezone.utc)
        with self._lock:
            entry = self._entries[name]
            entry.enabled = True
            entry.compute_next_run(now)

    def list_entries(self) -> list[ScheduleEntry]:
        """Snapshot of all registered entries."""
        with self._lock:
            return list(self._entries.values())

    def tick(self, now: datetime | None = None) -> list[str]:
        """Fire every due entry once; returns the names that fired."""
        now = now or datetime.now(timezone.utc)
        fired: list[str] = []
        with self._lock:
            for entry in self._entries.values():
                if not entry.enabled:
                    continue
                if entry.next_run is None:
                    entry.compute_next_run(now)
                    continue
                if entry.next_run <= now:
                    entry.last_run = now
                    entry.compute_next_run(now)
                    fired.append(entry.name)
                    try:
                        if entry.callback is not None:
                            entry.callback(entry.pipeline_name)
                        LOG.info("schedule '%s' fired for pipeline '%s'", entry.name, entry.pipeline_name)
                    except Exception:
                        LOG.exception("schedule '%s' callback failed", entry.name)
        return fired

    def run_forever(self, interval: float = 30.0, stop_event: threading.Event | None = None) -> None:
        """Blocking scheduler loop; suitable for worker processes."""
        stop = stop_event or threading.Event()
        LOG.info("scheduler loop started (interval=%.1fs)", interval)
        while not stop.is_set():
            started = time.monotonic()
            self.tick()
            elapsed = time.monotonic() - started
            stop.wait(max(0.0, interval - elapsed))

    def start_background(self, interval: float = 30.0) -> threading.Event:
        """Run :meth:`run_forever` on a daemon thread; returns its stop event."""
        stop_event = threading.Event()
        thread = threading.Thread(
            target=self.run_forever,
            kwargs={"interval": interval, "stop_event": stop_event},
            name="devops-scheduler",
            daemon=True,
        )
        thread.start()
        return stop_event


class TriggerManager:
    """Routes inbound events to pipelines whose triggers match."""

    def __init__(self) -> None:
        self._pipelines: dict[str, Pipeline] = {}

    def register(self, pipeline: Pipeline) -> None:
        """Register (or replace) a pipeline for event routing."""
        self._pipelines[pipeline.name] = pipeline

    def unregister(self, name: str) -> bool:
        """Remove a pipeline; False when unknown."""
        return self._pipelines.pop(name, None) is not None

    @property
    def pipelines(self) -> list[str]:
        """Registered pipeline names."""
        return sorted(self._pipelines)

    def dispatch(self, event: dict[str, Any], ref: str | None = None) -> list[str]:
        """Return pipeline names triggered by ``event``."""
        matched: list[str] = []
        for pipeline in self._pipelines.values():
            for trigger in pipeline.triggers:
                if trigger.kind != "manual" and trigger.matches(event=event, ref=ref):
                    matched.append(pipeline.name)
                    break
        return matched


if __name__ == "__main__":  # pragma: no cover - manual demo entry point
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    scheduler = Scheduler()

    def announce(pipeline: str) -> None:
        LOG.info("would run pipeline: %s", pipeline)

    scheduler.schedule_pipeline("nightly-build", "0 3 * * *", announce)
    scheduler.run_forever(interval=60.0)
