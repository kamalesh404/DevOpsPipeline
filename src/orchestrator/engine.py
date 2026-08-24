"""Core pipeline execution engine.

The engine validates a :class:`~src.orchestrator.pipeline.Pipeline`, computes
its dependency graph, and executes stages concurrently (up to
``max_parallel``) while guaranteeing a stage only starts after all of its
dependencies succeeded. Failed stages cascade-skip their downstream work.
"""

from __future__ import annotations

import itertools
import logging
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from src.orchestrator.pipeline import Pipeline, PipelineValidationError, Trigger
from src.orchestrator.stage import (
    CommandFn,
    ExecutionContext,
    StageResult,
    StageStatus,
)
from src.runners.base import JobSpec, Runner

LOG = logging.getLogger("devopspipeline.engine")

EventListener = Callable[[str, dict[str, Any]], None]
ExecutorFactory = Callable[[ExecutionContext], CommandFn]


@dataclass
class PipelineRun:
    """Aggregate state and results for one execution of a pipeline."""

    id: str
    pipeline: str
    trigger: str
    status: StageStatus = StageStatus.PENDING
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    results: dict[str, StageResult] = field(default_factory=dict)
    event: Optional[dict[str, Any]] = None

    @property
    def duration(self) -> float:
        """Wall-clock seconds from start until finish (or now)."""
        return (self.finished_at or time.time()) - self.started_at

    @property
    def success(self) -> bool:
        """True when every executed stage succeeded."""
        return self.status is StageStatus.SUCCESS

    def to_dict(self) -> dict[str, Any]:
        """Serialize the run for API responses and journaling."""
        return {
            "id": self.id,
            "pipeline": self.pipeline,
            "trigger": self.trigger,
            "status": self.status.value,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration": round(self.duration, 4),
            "event": self.event,
            "results": {name: result.to_dict() for name, result in self.results.items()},
        }


class PipelineEngine:
    """Executes pipelines against a runner with bounded parallelism.

    Parameters
    ----------
    runner:
        Any :class:`~src.runners.base.Runner` implementation used for command
        execution.
    max_parallel:
        Upper bound on concurrently executing stages.
    plugins:
        Optional plugin manager; lifecycle events are forwarded to it.
    on_event:
        Optional callback invoked as ``on_event(kind, payload)`` for
        ``pipeline_start``, ``stage_complete`` and ``pipeline_complete``.
    executor_factory:
        Testing/DI hook replacing the default runner-bound executor.
    workspace_root:
        Base directory under which per-run workspaces are created.
    """

    def __init__(
        self,
        runner: Runner,
        *,
        max_parallel: int = 4,
        plugins: Any = None,
        on_event: Optional[EventListener] = None,
        executor_factory: Optional[ExecutorFactory] = None,
        workspace_root: str | Path | None = None,
    ) -> None:
        self.runner = runner
        self.max_parallel = max(1, int(max_parallel))
        self.plugins = plugins
        self.on_event = on_event
        self.executor_factory = executor_factory
        base = Path(workspace_root) if workspace_root else Path(runner.workspace_root)
        self.workspace_root = base / "runs"
        self._sequence = itertools.count(1)

    def _emit(self, kind: str, payload: dict[str, Any]) -> None:
        """Fan an engine event out to listeners and plugins."""
        if self.on_event is not None:
            try:
                self.on_event(kind, payload)
            except Exception:  # listener bugs must not break runs
                LOG.exception("event listener raised for %s", kind)
        if self.plugins is not None:
            dispatch = {
                "pipeline_start": getattr(self.plugins, "fire_pipeline_start", None),
                "pipeline_complete": getattr(self.plugins, "fire_pipeline_complete", None),
            }.get(kind)
            if dispatch is not None:
                dispatch(payload)

    def _bind_executor(self, ctx: ExecutionContext) -> CommandFn:
        """Create the CommandFn that routes commands through the runner."""

        def execute(commands: Any, env: Any, timeout: Any) -> Any:
            spec = JobSpec(
                id=f"{ctx.run_id}-{next(self._sequence)}",
                commands=[str(command) for command in commands],
                env=dict(env) if env else ctx.environment,
                workspace=ctx.workspace,
                timeout=float(timeout) if timeout else None,
            )
            return self.runner.execute(spec)

        return execute

    def _build_context(
        self,
        run: PipelineRun,
        environment: Optional[dict[str, str]],
    ) -> ExecutionContext:
        """Assemble the execution context for a fresh run."""
        workspace = (self.workspace_root / run.id).resolve()
        workspace.mkdir(parents=True, exist_ok=True)
        ctx = ExecutionContext(
            run_id=run.id,
            pipeline_name=run.pipeline,
            workspace=workspace,
            environment={**(environment or {})},
            variables=dict(run.event or {}),
            event=run.event,
        )
        ctx.executor = (
            self.executor_factory(ctx) if self.executor_factory else self._bind_executor(ctx)
        )
        return ctx

    def _cascade_skips(
        self,
        pipeline: Pipeline,
        completed: dict[str, StageResult],
        pending: list[str],
        run: PipelineRun,
    ) -> None:
        """Mark every transitive dependent of failed stages as SKIPPED."""
        bad = {name for name, result in completed.items() if not result.succeeded}
        changed = True
        while changed and pending:
            changed = False
            for name in list(pending):
                deps = pipeline.stage_map[name].depends_on
                if any(dep in bad for dep in deps):
                    pending.remove(name)
                    bad.add(name)
                    result = StageResult(name=name, status=StageStatus.SKIPPED)
                    completed[name] = result
                    run.results[name] = result
                    LOG.info("[%s] stage '%s' skipped (upstream failure)", run.id, name)
                    self._emit("stage_complete", result.to_dict())
                    changed = True

    def run(
        self,
        pipeline: Pipeline,
        *,
        trigger: Optional[Trigger] = None,
        event: Optional[dict[str, Any]] = None,
        environment: Optional[dict[str, str]] = None,
    ) -> PipelineRun:
        """Execute ``pipeline`` end-to-end and return the resulting run."""
        errors = []
        try:
            pipeline.validate()
        except PipelineValidationError as exc:
            errors = exc.errors
        if errors:
            raise PipelineValidationError(errors)

        run = PipelineRun(
            id=uuid.uuid4().hex[:12],
            pipeline=pipeline.name,
            trigger=trigger.kind if trigger else "manual",
            status=StageStatus.RUNNING,
            event=event,
        )
        ctx = self._build_context(run, environment)
        order = pipeline.topological_order()
        pending: list[str] = list(order)
        completed: dict[str, StageResult] = {}
        running: dict[Future[StageResult], str] = {}

        LOG.info("[%s] starting pipeline '%s' (%d stages)", run.id, pipeline.name, len(order))
        self._emit("pipeline_start", run.to_dict())

        with ThreadPoolExecutor(max_workers=self.max_parallel, thread_name_prefix="stage") as pool:
            while pending or running:
                progressed = True
                while progressed:
                    progressed = False
                    done_names = {n for n, r in completed.items() if r.succeeded}
                    for name in list(pending):
                        deps = set(pipeline.stage_map[name].depends_on)
                        if deps <= done_names:
                            pending.remove(name)
                            running[pool.submit(pipeline.stage_map[name].run, ctx)] = name
                            progressed = True
                self._cascade_skips(pipeline, completed, pending, run)
                if not running:
                    if pending:  # pragma: no cover - validation prevents this
                        raise RuntimeError(f"scheduler stalled with pending stages: {pending}")
                    break
                finished, _ = wait(set(running), return_when=FIRST_COMPLETED)
                for future in finished:
                    name = running.pop(future)
                    try:
                        result = future.result()
                    except Exception as exc:  # defensive: isolate crashes
                        result = StageResult(name=name, status=StageStatus.FAILED, error=str(exc))
                    completed[name] = result
                    run.results[name] = result
                    LOG.info(
                        "[%s] stage '%s' -> %s (%.2fs)",
                        run.id,
                        name,
                        result.status.value,
                        result.duration,
                    )
                    self._emit("stage_complete", result.to_dict())

        run.finished_at = time.time()
        all_ok = bool(run.results) and all(r.succeeded for r in run.results.values())
        run.status = StageStatus.SUCCESS if all_ok else StageStatus.FAILED
        LOG.info("[%s] pipeline '%s' finished: %s (%.2fs)", run.id, pipeline.name, run.status.value, run.duration)
        self._emit("pipeline_complete", run.to_dict())
        return run
