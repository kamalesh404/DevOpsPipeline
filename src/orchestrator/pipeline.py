"""Declarative pipeline model.

A :class:`Pipeline` is an ordered collection of stages plus the triggers that
can start it. The module provides a lightweight ``CommandStage`` for
YAML-defined pipelines, trigger matching, validation (duplicate names, unknown
dependencies, cycles) and deterministic topological ordering.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional

import yaml

from src.orchestrator.stage import BaseStage, ExecutionContext, RetryPolicy, StageResult, StageStatus

TRIGGER_KINDS = frozenset({"manual", "webhook", "schedule", "push", "tag"})


@dataclass
class Trigger:
    """A condition that can start a pipeline run."""

    kind: str = "manual"
    branches: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    events: tuple[str, ...] = ()
    schedule: Optional[str] = None
    enabled: bool = True

    def __post_init__(self) -> None:
        if self.kind not in TRIGGER_KINDS:
            raise ValueError(f"unknown trigger kind '{self.kind}'; expected one of {sorted(TRIGGER_KINDS)}")

    def matches(self, event: Optional[Mapping[str, Any]] = None, ref: Optional[str] = None) -> bool:
        """Return True when this trigger should fire for ``event``/``ref``."""
        if not self.enabled:
            return False
        if self.kind == "manual":
            return event is None
        if self.kind in {"webhook", "push"}:
            if not event:
                return False
            kind = str(event.get("kind", "push" if self.kind == "push" else "webhook"))
            if self.events and kind not in self.events:
                return False
            branch = ref or str(event.get("branch", ""))
            if self.branches and not any(fnmatch.fnmatch(branch, pat) for pat in self.branches):
                return False
            return True
        if self.kind == "tag":
            tag_ref = ref or str(event.get("tag", "") if event else "")
            tags = self.tags or ("*",)
            return any(fnmatch.fnmatch(tag_ref.removeprefix("refs/tags/"), pat) for pat in tags)
        return False  # schedule triggers are driven by the Scheduler, not events

    def to_dict(self) -> dict[str, Any]:
        """Serialize the trigger for storage/API responses."""
        return {
            "kind": self.kind,
            "branches": list(self.branches),
            "tags": list(self.tags),
            "events": list(self.events),
            "schedule": self.schedule,
            "enabled": self.enabled,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Trigger":
        """Rebuild a trigger from its dictionary form."""
        return cls(
            kind=str(data.get("kind", "manual")),
            branches=tuple(data.get("branches", ())),
            tags=tuple(data.get("tags", ())),
            events=tuple(data.get("events", ())),
            schedule=data.get("schedule"),
            enabled=bool(data.get("enabled", True)),
        )


@dataclass
class CommandStage(BaseStage):
    """A stage that simply runs a list of shell commands."""

    commands: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        super().__post_init__()
        self.commands = tuple(str(command) for command in self.commands)

    def build_commands(self, ctx: ExecutionContext) -> list[str]:
        """Return the configured shell commands verbatim."""
        return list(self.commands)

    @classmethod
    def from_spec(cls, spec: Mapping[str, Any]) -> "CommandStage":
        """Build from a YAML stage mapping with ``script``/``command`` keys."""
        raw = spec.get("script") or spec.get("commands") or spec.get("command") or []
        commands = [raw] if isinstance(raw, str) else [str(item) for item in raw]
        retry = spec.get("retry") or {}
        return cls(
            name=str(spec["name"]),
            commands=tuple(commands),
            depends_on=tuple(spec.get("depends_on", ()) or ()),
            timeout=spec.get("timeout"),
            retry_policy=RetryPolicy(
                max_attempts=int(retry.get("attempts", 1)),
                initial_delay=float(retry.get("delay", 1.0)),
            ),
        )


class PipelineValidationError(ValueError):
    """Raised when a pipeline definition is structurally invalid."""

    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


@dataclass
class Pipeline:
    """An executable CI/CD pipeline definition."""

    name: str
    description: str = ""
    stages: list[BaseStage] = field(default_factory=list)
    triggers: list[Trigger] = field(default_factory=list)
    environment: dict[str, str] = field(default_factory=dict)
    max_parallel: int = 4
    timeout: Optional[float] = None
    version: int = 1

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]*", self.name):
            raise ValueError(f"invalid pipeline name '{self.name}'")

    @property
    def stage_names(self) -> list[str]:
        """Ordered list of stage names."""
        return [stage.name for stage in self.stages]

    @property
    def stage_map(self) -> dict[str, BaseStage]:
        """Mapping of stage name to stage instance."""
        return {stage.name: stage for stage in self.stages}

    def validate(self) -> None:
        """Raise :class:`PipelineValidationError` listing all problems found."""
        errors: list[str] = []
        seen: set[str] = set()
        for stage in self.stages:
            if stage.name in seen:
                errors.append(f"duplicate stage name '{stage.name}'")
            seen.add(stage.name)
        for stage in self.stages:
            for dependency in stage.depends_on:
                if dependency not in seen:
                    errors.append(f"stage '{stage.name}' depends on unknown stage '{dependency}'")
        if not self.stages:
            errors.append("pipeline has no stages")
        try:
            self.topological_order()
        except PipelineValidationError as exc:
            errors.extend(exc.errors)
        if errors:
            raise PipelineValidationError(errors)

    def topological_order(self) -> list[str]:
        """Kahn's algorithm preserving definition order; detects cycles."""
        indegree = {stage.name: 0 for stage in self.stages}
        dependents: dict[str, list[str]] = {stage.name: [] for stage in self.stages}
        for stage in self.stages:
            for dependency in stage.depends_on:
                if dependency in indegree:
                    indegree[stage.name] += 1
                    dependents[dependency].append(stage.name)
        order: list[str] = []
        ready = [s.name for s in self.stages if indegree[s.name] == 0]
        while ready:
            current = ready.pop(0)
            order.append(current)
            for dependent in dependents[current]:
                indegree[dependent] -= 1
                if indegree[dependent] == 0:
                    ready.append(dependent)
        if len(order) != len(self.stages):
            cyclic = sorted(set(self.stage_names) - set(order))
            raise PipelineValidationError([f"dependency cycle involving stages {cyclic}"])
        return order

    def downstream_of(self, names: set[str]) -> set[str]:
        """All stages transitively depending on any name in ``names``."""
        affected: set[str] = set()
        changed = True
        while changed:
            changed = False
            for stage in self.stages:
                if stage.name in affected:
                    continue
                if any(dep in names or dep in affected for dep in stage.depends_on):
                    affected.add(stage.name)
                    changed = True
        return affected - names

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON/YAML-friendly dictionary."""
        serialized_stages: list[dict[str, Any]] = []
        for stage in self.stages:
            entry: dict[str, Any] = {
                "name": stage.name,
                "depends_on": list(stage.depends_on),
                "timeout": stage.timeout,
            }
            if isinstance(stage, CommandStage):
                entry["type"] = "command"
                entry["commands"] = list(stage.commands)
            else:
                entry["type"] = f"{type(stage).__module__}.{type(stage).__name__}"
            serialized_stages.append(entry)
        return {
            "version": self.version,
            "name": self.name,
            "description": self.description,
            "environment": dict(self.environment),
            "max_parallel": self.max_parallel,
            "timeout": self.timeout,
            "triggers": [trigger.to_dict() for trigger in self.triggers],
            "stages": serialized_stages,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Pipeline":
        """Rebuild a pipeline from its dictionary form."""
        stages: list[BaseStage] = []
        for spec in data.get("stages", []):
            stage_type = str(spec.get("type", "command"))
            if stage_type != "command":
                raise ValueError(
                    f"cannot deserialize stage type '{stage_type}'; only 'command' is supported here"
                )
            stages.append(CommandStage.from_spec(spec))
        return cls(
            name=str(data["name"]),
            description=str(data.get("description", "")),
            stages=stages,
            triggers=[Trigger.from_dict(t) for t in data.get("triggers", [])],
            environment={str(k): str(v) for k, v in data.get("environment", {}).items()},
            max_parallel=int(data.get("max_parallel", 4)),
            timeout=data.get("timeout"),
        )

    @classmethod
    def from_yaml_file(cls, path: str | Path) -> "Pipeline":
        """Load and validate a pipeline from a YAML file on disk."""
        payload = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        pipeline = cls.from_dict(payload or {})
        pipeline.validate()
        return pipeline


def summarize_result(result: StageResult) -> str:
    """One-line human summary used by CLI output."""
    marker = "OK " if result.succeeded else result.status.value
    return f"{marker:<8} {result.name:<24} exit={result.exit_code:<4} {result.duration:.2f}s"
