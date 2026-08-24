"""Pipeline management commands: create, validate, run and inspect."""

from __future__ import annotations

import json
import logging
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

import click
import yaml

from src.orchestrator.engine import PipelineEngine
from src.orchestrator.pipeline import CommandStage, Pipeline
from src.runners.local import LocalRunner
from src.stages.build import BuildStage
from src.stages.test import TestStage

LOG = logging.getLogger("devopspipeline.cli.pipeline")

STATE_DIR = Path.home() / ".devopspipeline"
STATE_FILE = STATE_DIR / "state.json"
JOURNAL_FILE = STATE_DIR / "runs.jsonl"


class StateStore:
    """Tiny JSON document store persisting pipeline definitions."""

    def __init__(self, path: Path = STATE_FILE) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load_all(self) -> dict[str, Any]:
        """Read the whole state file (empty when missing/corrupt)."""
        if not self.path.exists():
            return {"pipelines": {}}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            LOG.warning("state file corrupt; starting fresh")
            return {"pipelines": {}}

    def save_pipeline(self, pipeline: Pipeline) -> None:
        """Persist one pipeline definition."""
        state = self.load_all()
        state.setdefault("pipelines", {})[pipeline.name] = pipeline.to_dict()
        self.path.write_text(json.dumps(state, indent=2), encoding="utf-8")

    def load_pipeline(self, name: str) -> Optional[Pipeline]:
        """Rebuild a stored pipeline or None."""
        data = self.load_all().get("pipelines", {}).get(name)
        return Pipeline.from_dict(data) if data else None

    def delete_pipeline(self, name: str) -> bool:
        """Remove a stored pipeline; False when absent."""
        state = self.load_all()
        existed = state.get("pipelines", {}).pop(name, None) is not None
        if existed:
            self.path.write_text(json.dumps(state, indent=2), encoding="utf-8")
        return existed


def _append_journal(run_dict: dict[str, Any]) -> None:
    """Append a finished run to the local JSONL journal."""
    JOURNAL_FILE.parent.mkdir(parents=True, exist_ok=True)
    with JOURNAL_FILE.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(run_dict) + "\n")


@click.group()
def pipeline_group() -> None:
    """Create, inspect and run CI/CD pipelines."""


@pipeline_group.command("create")
@click.argument("name")
@click.option("--description", default="", show_default=True)
@click.option("--command", "commands", multiple=True, help="Shell command for a stage; repeatable.")
@click.option("--stage-name", default="main", show_default=True)
def create(name: str, description: str, commands: tuple[str, ...], stage_name: str) -> None:
    """Create a simple single-stage pipeline."""
    stage = CommandStage(name=stage_name, commands=tuple(commands))
    pipeline = Pipeline(name=name, description=description, stages=[stage])
    pipeline.validate()
    StateStore().save_pipeline(pipeline)
    click.echo(f"created pipeline '{name}' with {len(commands)} command(s)")


@pipeline_group.command("list")
def list_pipelines() -> None:
    """List stored pipelines."""
    pipelines = StateStore().load_all().get("pipelines", {})
    if not pipelines:
        click.echo("no pipelines stored; use 'devops pipeline create'")
        return
    for name in sorted(pipelines):
        stages = len(pipelines[name].get("stages", []))
        click.echo(f"{name:<28} {stages} stage(s)")


@pipeline_group.command("show")
@click.argument("name")
def show(name: str) -> None:
    """Print one pipeline's YAML definition."""
    pipeline = StateStore().load_pipeline(name)
    if pipeline is None:
        raise click.ClickException(f"pipeline '{name}' not found")
    click.echo(yaml.safe_dump(pipeline.to_dict(), sort_keys=False))


@pipeline_group.command("delete")
@click.argument("name")
def delete(name: str) -> None:
    """Delete a stored pipeline."""
    if not StateStore().delete_pipeline(name):
        raise click.ClickException(f"pipeline '{name}' not found")
    click.echo(f"deleted '{name}'")


@pipeline_group.command("validate")
@click.argument("path", type=click.Path(exists=True, dir_okay=False))
def validate(path: str) -> None:
    """Validate a pipeline YAML file without running it."""
    try:
        loaded = Pipeline.from_yaml_file(path)
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc
    click.secho(f"OK — {loaded.name}: {len(loaded.stages)} stage(s), {len(loaded.triggers)} trigger(s)", fg="green")


@pipeline_group.command("run")
@click.argument("name", required=False)
@click.option("--from-file", "from_file", type=click.Path(exists=True, dir_okay=False))
@click.option("--set", "overrides", multiple=True, help="Environment override KEY=VALUE.")
@click.option("--keep-workspaces/--purge-workspaces", default=False, show_default=True)
def run(name: Optional[str], from_file: Optional[str], overrides: tuple[str, ...], keep_workspaces: bool) -> None:
    """Run a stored pipeline (or --from-file YAML)."""
    if from_file:
        pipeline = Pipeline.from_yaml_file(from_file)
    elif name:
        loaded = StateStore().load_pipeline(name)
        if loaded is None:
            raise click.ClickException(f"pipeline '{name}' not found")
        pipeline = loaded
    else:
        raise click.UsageError("provide NAME or --from-file")

    environment = dict(override.split("=", 1) for override in overrides if "=" in override)
    runner = LocalRunner(
        workspace_root=Path(tempfile.gettempdir()) / "devopspipeline-cli",
        keep_workspaces=keep_workspaces,
    )
    engine = PipelineEngine(runner, max_parallel=pipeline.max_parallel)
    started = time.time()
    finished = engine.run(pipeline, environment=environment)
    _append_journal(finished.to_dict())

    for result in finished.results.values():
        marker = click.style(result.status.value, fg="green" if result.succeeded else "red")
        click.echo(f"{marker:<10} {result.name:<24} exit={result.exit_code:<4} {result.duration:.2f}s")
    total = time.time() - started
    color = "green" if finished.success else "red"
    click.secho(f"run {finished.id} finished in {total:.2f}s: {finished.status.value}", fg=color)
    if not finished.success:
        sys.exit(1)


@pipeline_group.command("logs")
@click.argument("run_id")
@click.option("--tail", "-n", default=50, show_default=True, type=int)
def logs(run_id: str, tail: int) -> None:
    """Show captured output of a previous run from the journal."""
    if not JOURNAL_FILE.exists():
        raise click.ClickException("no runs journaled yet")
    entry: dict[str, Any] | None = None
    for line in JOURNAL_FILE.read_text(encoding="utf-8").splitlines():
        try:
            candidate = json.loads(line)
        except json.JSONDecodeError:
            continue
        if candidate.get("id") == run_id:
            entry = candidate
    if entry is None:
        raise click.ClickException(f"run '{run_id}' not found in journal")
    for result in entry.get("results", {}).values():
        click.echo(click.style(f"--- {result['name']} [{result['status']}] ---", bold=True))
        lines = result.get("output", "").splitlines()[-tail:]
        click.echo("\n".join(lines) or "(no output)")


@pipeline_group.command("scaffold")
@click.argument("path", type=click.Path(dir_okay=False))
@click.option("--framework", default="python", type=click.Choice(["python", "node"]), show_default=True)
def scaffold(path: str, framework: str) -> None:
    """Write a starter pipeline.yaml mixing built-in stage types."""
    test_stage = TestStage(name="test", framework="pytest" if framework == "python" else "jest", coverage_min=60.0)
    build_stage = BuildStage(name="build", system="make", artifact_globs=("dist/**",))
    document: dict[str, Any] = {
        "name": "starter",
        "description": f"Scaffolded {framework} pipeline",
        "max_parallel": 2,
        "triggers": [{"kind": "push", "branches": ["main"]}],
        "stages": [
            {"type": "command", "name": "checkout", "script": ["git clone --depth 1 . ."]},
            {"type": "custom", "ref": "test"},
            {"type": "custom", "ref": "build", "depends_on": ["test"]},
        ],
    }
    Path(path).write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    sidecar = Path(path).with_suffix(".stages.json")
    sidecar.write_text(json.dumps({"test": test_stage.__dict__, "build": build_stage.__dict__}, indent=2, default=str), encoding="utf-8")
    click.echo(f"wrote {path} (+{sidecar.name}); customize before running")
