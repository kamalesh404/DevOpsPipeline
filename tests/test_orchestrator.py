"""Tests for the orchestration layer: pipeline model, engine and scheduler."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.orchestrator.engine import PipelineEngine, PipelineRun
from src.orchestrator.pipeline import CommandStage, Pipeline, PipelineValidationError, Trigger
from src.orchestrator.scheduler import CronTrigger, Scheduler, ScheduleEntry, TriggerManager
from src.orchestrator.stage import ParallelGroup, RetryPolicy, StageResult, StageStatus
from tests.conftest import RecordingRunner


# --------------------------------------------------------------------------- #
# pipeline model
# --------------------------------------------------------------------------- #
def test_topological_order_respects_dependencies() -> None:
    pipeline = Pipeline(
        name="order",
        stages=[
            CommandStage(name="d", commands=["x"], depends_on=("b",)),
            CommandStage(name="a", commands=["x"]),
            CommandStage(name="b", commands=["x"], depends_on=("a",)),
        ],
    )
    order = pipeline.topological_order()
    assert set(order) == {"a", "b", "d"}
    assert order.index("a") < order.index("b") < order.index("d")


def test_cycle_detection_raises() -> None:
    pipeline = Pipeline(
        name="cyclic",
        stages=[
            CommandStage(name="a", commands=["x"], depends_on=("b",)),
            CommandStage(name="b", commands=["x"], depends_on=("a",)),
        ],
    )
    with pytest.raises(PipelineValidationError) as excinfo:
        pipeline.validate()
    assert any("cycle" in error for error in excinfo.value.errors)


def test_unknown_dependency_rejected() -> None:
    pipeline = Pipeline(name="baddep", stages=[CommandStage(name="a", commands=["x"], depends_on=("ghost",))])
    with pytest.raises(PipelineValidationError):
        pipeline.validate()


def test_duplicate_stage_names_rejected() -> None:
    pipeline = Pipeline(
        name="dupe",
        stages=[CommandStage(name="same", commands=["x"]), CommandStage(name="same", commands=["y"])],
    )
    with pytest.raises(PipelineValidationError):
        pipeline.validate()


def test_pipeline_roundtrip_via_dict(sample_pipeline: Pipeline) -> None:
    rebuilt = Pipeline.from_dict(sample_pipeline.to_dict())
    assert rebuilt.stage_names == sample_pipeline.stage_names
    assert rebuilt.stages[3].depends_on == ("b", "c")


def test_trigger_matching_branch_globs() -> None:
    trigger = Trigger(kind="push", branches=("release/*",))
    assert trigger.matches(event={"kind": "push", "branch": "release/2.1"})
    assert not trigger.matches(event={"kind": "push", "branch": "feature/x"})


# --------------------------------------------------------------------------- #
# engine execution
# --------------------------------------------------------------------------- #
def test_engine_runs_all_stages_successfully(engine: PipelineEngine, sample_pipeline: Pipeline) -> None:
    run = engine.run(sample_pipeline)
    assert isinstance(run, PipelineRun)
    assert run.status is StageStatus.SUCCESS
    assert set(run.results) == {"a", "b", "c", "d"}
    assert all(result.succeeded for result in run.results.values())


def test_engine_skips_downstream_after_failure(recording_runner: object) -> None:
    runner = recording_runner
    assert isinstance(runner, RecordingRunner)
    runner.fail_patterns = ("boom",)
    engine = PipelineEngine(runner, max_parallel=2)
    pipeline = Pipeline(
        name="cascade",
        stages=[
            CommandStage(name="ok", commands=["pass"]),
            CommandStage(name="explode", commands=["boom"], depends_on=("ok",)),
            CommandStage(name="after", commands=["pass"], depends_on=("explode",)),
        ],
    )
    run = engine.run(pipeline)
    assert run.status is StageStatus.FAILED
    assert run.results["explode"].status is StageStatus.FAILED
    assert run.results["after"].status is StageStatus.SKIPPED
    # Only 'ok' ever reached the runner; 'after' was skipped before execution.
    executed = [spec.joined_commands for spec in runner.seen]
    assert executed == ["pass", "boom"]


def test_engine_emits_lifecycle_events(fake_executor: object, tmp_path: object, sample_pipeline: Pipeline) -> None:
    from pathlib import Path

    events: list[tuple[str, dict]] = []
    executor, _calls = fake_executor(stdout="fine")
    engine = PipelineEngine(
        RecordingStub(),
        max_parallel=2,
        on_event=lambda kind, payload: events.append((kind, payload)),
        executor_factory=lambda ctx: executor,
        workspace_root=Path(str(tmp_path)) / "event-ws",
    )
    run = engine.run(sample_pipeline)
    kinds = [kind for kind, _payload in events]
    assert run.success
    assert kinds[0] == "pipeline_start"
    assert kinds.count("stage_complete") == 4
    assert kinds[-1] == "pipeline_complete"


class RecordingStub:
    """Placeholder runner never invoked because executor_factory overrides."""

    name = "stub"

    def __init__(self) -> None:
        from pathlib import Path

        self.workspace_root: Path = Path("/tmp/unused")

    def execute(self, spec):  # pragma: no cover - must never be reached
        raise AssertionError("runner should not be called when executor_factory is set")

    def healthcheck(self) -> bool:
        return False


def test_retry_policy_retries_until_success(fake_executor: object) -> None:
    from src.orchestrator.stage import ExecutionContext

    outcomes = iter([(1,), (0,)])

    class FlakyExecutor:
        calls = 0

        def __call__(self, commands, env, timeout):  # noqa: ANN001
            FlakyExecutor.calls += 1
            code = next(outcomes)[0]
            from types import SimpleNamespace

            return SimpleNamespace(exit_code=code, stdout="", stderr="")

    stage = CommandStage(
        name="flaky", commands=["x"], retry_policy=RetryPolicy(max_attempts=2, initial_delay=0.01)
    )
    ctx = ExecutionContext(run_id="r", pipeline_name="p", workspace="/tmp/x", executor=FlakyExecutor())
    result = stage.run(ctx)
    assert result.attempts == 2
    assert result.succeeded


def test_parallel_group_reports_child_failures(execution_context: object) -> None:
    from types import SimpleNamespace

    ctx = execution_context

    def flaky_executor(commands, env, timeout):  # noqa: ANN001
        joined = " ".join(commands)
        return SimpleNamespace(exit_code=1 if "fail" in joined else 0, stdout=joined, stderr="")

    ctx.executor = flaky_executor
    group = ParallelGroup.of(
        CommandStage(name="good", commands=["echo ok"]),
        CommandStage(name="bad", commands=["fail hard"]),
        name="group",
    )
    result = group.run(ctx)
    assert result.status is StageStatus.FAILED
    assert result.metadata["children"] == {"good": "SUCCESS", "bad": "FAILED"}


# --------------------------------------------------------------------------- #
# scheduler & triggers
# --------------------------------------------------------------------------- #
def test_cron_trigger_next_run() -> None:
    trigger = CronTrigger("*/5 * * * *")
    moment = datetime(2026, 8, 24, 12, 3, tzinfo=timezone.utc)
    nxt = trigger.next_after(moment)
    assert (nxt.hour, nxt.minute) == (12, 5)


def test_cron_trigger_rejects_invalid_expression() -> None:
    with pytest.raises(ValueError):
        CronTrigger("not a cron")


def test_scheduler_fires_due_entries() -> None:
    fired: list[str] = []
    scheduler = Scheduler()
    scheduler.schedule_pipeline("nightly", "* * * * *", callback=fired.append)
    now = datetime.now(timezone.utc)
    fired_now = scheduler.tick(now=scheduler.list_entries()[0].next_run or now)
    assert fired_now, "entry should fire on its scheduled minute"
    assert fired == ["nightly"]


def test_scheduler_pause_and_resume() -> None:
    scheduler = Scheduler()
    entry = scheduler.schedule_pipeline("weekly", "0 0 * * 0")
    scheduler.pause(entry.name)
    assert all(not e.enabled for e in scheduler.list_entries())
    scheduler.resume(entry.name)
    assert scheduler.list_entries()[0].enabled


def test_schedule_entry_duplicate_rejected() -> None:
    scheduler = Scheduler()
    scheduler.add_entry(ScheduleEntry(name="dup", pipeline_name="p", trigger=CronTrigger("@daily")))
    with pytest.raises(ValueError):
        scheduler.add_entry(ScheduleEntry(name="dup", pipeline_name="q", trigger=CronTrigger("@daily")))


def test_trigger_manager_dispatch() -> None:
    manager = TriggerManager()
    push_only = Pipeline(
        name="web",
        triggers=[Trigger(kind="push", branches=("main",))],
        stages=[CommandStage(name="build", commands=["make"])],
    )
    manual = Pipeline(name="ops", stages=[CommandStage(name="noop", commands=["true"])])
    manager.register(push_only)
    manager.register(manual)
    assert manager.dispatch({"kind": "push", "branch": "main"}) == ["web"]
    assert manager.dispatch({"kind": "push", "branch": "develop"}) == []
    assert manager.pipelines == ["ops", "web"]
