"""Stage primitives for the DevOpsPipeline orchestration layer.

This module defines the execution contract shared by every stage: lifecycle
statuses, retry semantics, the :class:`ExecutionContext` handed to stages at
run time, and the :class:`BaseStage` abstraction that concrete stages extend.
Composite stages (sequential/parallel groups) are also provided for grouping.
"""

from __future__ import annotations

import logging
import os
import time
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

LOG = logging.getLogger("devopspipeline.stage")


class StageStatus(str, Enum):
    """Lifecycle states for a single stage or an entire pipeline run."""

    PENDING = "PENDING"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    SKIPPED = "SKIPPED"

    @property
    def is_terminal(self) -> bool:
        """Return True when no further transitions are possible."""
        return self in {
            StageStatus.SUCCESS,
            StageStatus.FAILED,
            StageStatus.CANCELLED,
            StageStatus.SKIPPED,
        }

    @property
    def succeeded(self) -> bool:
        """Return True only for SUCCESS."""
        return self is StageStatus.SUCCESS


@dataclass
class RetryPolicy:
    """Exponential-backoff retry configuration for a stage.

    ``delay_for(n)`` returns the sleep before attempt ``n+1``; attempt 1 runs
    immediately, attempt 2 waits ``initial_delay``, then grows geometrically.
    """

    max_attempts: int = 1
    backoff_factor: float = 2.0
    initial_delay: float = 1.0
    max_delay: float = 60.0

    def delay_for(self, attempt: int) -> float:
        """Return seconds to wait after a failed ``attempt`` (1-based)."""
        return min(self.initial_delay * (self.backoff_factor ** max(attempt - 1, 0)), self.max_delay)


@dataclass
class StageResult:
    """Outcome of executing one stage within a pipeline run."""

    name: str
    status: StageStatus = StageStatus.FAILED
    exit_code: int = -1
    output: str = ""
    error: str = ""
    duration: float = 0.0
    attempts: int = 1
    artifacts: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None

    @property
    def succeeded(self) -> bool:
        """Whether this stage completed successfully."""
        return self.status is StageStatus.SUCCESS

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-friendly dictionary."""
        return {
            "name": self.name,
            "status": self.status.value,
            "exit_code": self.exit_code,
            "output": self.output,
            "error": self.error,
            "duration": round(self.duration, 4),
            "attempts": self.attempts,
            "artifacts": list(self.artifacts),
            "metadata": dict(self.metadata),
        }


#: Callable bound to a runner by the engine; returns a runner-level result
#: object exposing ``exit_code`` / ``stdout`` / ``stderr`` attributes.
CommandFn = Callable[[Sequence[str], Optional[Mapping[str, str]], Optional[float]], Any]

SkipPredicate = Callable[["ExecutionContext"], bool]


@dataclass
class ExecutionContext:
    """Per-run context passed to every stage during execution."""

    run_id: str
    pipeline_name: str
    workspace: Path
    environment: dict[str, str] = field(default_factory=dict)
    variables: dict[str, Any] = field(default_factory=dict)
    event: Optional[dict[str, Any]] = None
    executor: Optional[CommandFn] = None
    logger: logging.Logger = field(default_factory=lambda: LOG)

    def child_env(self, extra: Optional[Mapping[str, Any]] = None) -> dict[str, str]:
        """Merge the parent process environment with pipeline/step values."""
        merged: dict[str, str] = {**os.environ, **self.environment}
        for key, value in (extra or {}).items():
            merged[str(key)] = str(value)
        return merged

    def variable(self, key: str, default: Any = None) -> Any:
        """Look up a trigger/event variable with an optional default."""
        return self.variables.get(key, default)


@dataclass
class BaseStage(ABC):
    """Abstract base class implementing retries, timing and skip logic.

    Concrete stages implement :meth:`build_commands` and may override
    :meth:`post_process` to parse command output or collect artifacts.
    """

    name: str
    depends_on: tuple[str, ...] = ()
    retry_policy: RetryPolicy = field(default_factory=RetryPolicy)
    timeout: Optional[float] = None
    skip_when: Optional[SkipPredicate] = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("stage name must be non-empty")
        self.depends_on = tuple(self.depends_on)

    @abstractmethod
    def build_commands(self, ctx: ExecutionContext) -> list[str]:
        """Return the shell commands that make up this stage."""

    def post_process(self, ctx: ExecutionContext, result: StageResult) -> StageResult:
        """Hook invoked after execution; mutate/annotate the result here."""
        return result

    def run(self, ctx: ExecutionContext) -> StageResult:
        """Execute the stage honoring skip predicates and retry policy."""
        if self.skip_when is not None and self.skip_when(ctx):
            return StageResult(name=self.name, status=StageStatus.SKIPPED, error="skip predicate matched")
        if ctx.executor is None:
            return StageResult(name=self.name, status=StageStatus.SKIPPED, error="no executor bound")

        commands = [str(command) for command in self.build_commands(ctx)]
        result = StageResult(name=self.name, status=StageStatus.FAILED, attempts=0)
        result.started_at = time.time()

        for attempt in range(1, self.retry_policy.max_attempts + 1):
            result.attempts = attempt
            outcome = ctx.executor(commands, None, self.timeout)
            result.exit_code = int(getattr(outcome, "exit_code", 1))
            result.output = str(getattr(outcome, "stdout", "") or "")
            stderr = str(getattr(outcome, "stderr", "") or "")
            result.error = stderr.strip()
            if result.exit_code == 0:
                result.status = StageStatus.SUCCESS
                break
            if attempt < self.retry_policy.max_attempts:
                time.sleep(min(self.retry_policy.delay_for(attempt), 5.0))

        result.finished_at = time.time()
        result.duration = (result.finished_at or 0.0) - (result.started_at or 0.0)
        return self.post_process(ctx, result)


@dataclass
class SequentialGroup(BaseStage):
    """Run child stages one-by-one, stopping at the first failure."""

    children: tuple["BaseStage", ...] = ()

    @classmethod
    def of(cls, *stages: BaseStage, name: str = "sequential-group") -> "SequentialGroup":
        """Build a sequential group from positional child stages."""
        return cls(name=name, children=tuple(stages))

    def build_commands(self, ctx: ExecutionContext) -> list[str]:  # pragma: no cover
        raise NotImplementedError("composite stages do not produce flat commands")

    def run(self, ctx: ExecutionContext) -> StageResult:
        started = time.time()
        outputs: list[str] = []
        artifacts: list[str] = []
        overall = StageStatus.SUCCESS
        for child in self.children:
            child_result = child.run(ctx)
            outputs.append(f"--- {child.name} [{child_result.status.value}] ---\n{child_result.output}")
            artifacts.extend(child_result.artifacts)
            if not child_result.succeeded:
                overall = child_result.status
                break
        return StageResult(
            name=self.name,
            status=overall,
            output="\n".join(outputs),
            duration=time.time() - started,
            artifacts=artifacts,
            attempts=1,
        )


@dataclass
class ParallelGroup(BaseStage):
    """Run independent child stages concurrently on a thread pool."""

    children: tuple["BaseStage", ...] = ()
    max_workers: int = 4

    @classmethod
    def of(cls, *stages: BaseStage, name: str = "parallel-group") -> "ParallelGroup":
        """Build a parallel group from positional child stages."""
        return cls(name=name, children=tuple(stages))

    def build_commands(self, ctx: ExecutionContext) -> list[str]:  # pragma: no cover
        raise NotImplementedError("composite stages do not produce flat commands")

    def run(self, ctx: ExecutionContext) -> StageResult:
        started = time.time()
        results: list[StageResult] = []
        with ThreadPoolExecutor(max_workers=max(1, min(self.max_workers, len(self.children)))) as pool:
            futures = [(child, pool.submit(child.run, ctx)) for child in self.children]
            for child, future in futures:
                try:
                    results.append(future.result())
                except Exception as exc:  # defensive: isolate child crashes
                    results.append(
                        StageResult(name=child.name, status=StageStatus.FAILED, error=str(exc))
                    )
        failed = [r.name for r in results if not r.succeeded]
        return StageResult(
            name=self.name,
            status=StageStatus.FAILED if failed else StageStatus.SUCCESS,
            error=f"failed children: {failed}" if failed else "",
            duration=time.time() - started,
            artifacts=[a for r in results for a in r.artifacts],
            metadata={"children": {r.name: r.status.value for r in results}},
        )
