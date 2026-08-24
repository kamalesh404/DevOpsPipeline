"""Lint stage for ruff, eslint and cargo clippy.

Configures tool invocations (with optional auto-fix), then extracts issue
counts from output into metadata so trend dashboards can chart code-quality
signals over time.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from src.orchestrator.stage import BaseStage, ExecutionContext, StageResult

_ISSUE_COUNT = re.compile(r"\b(\d+)\s+(?:error|warning|issue)s?\b", re.IGNORECASE)
_TOOLS = frozenset({"ruff", "eslint", "clippy"})


@dataclass
class LintStage(BaseStage):
    """Run static analysis over source and test code."""

    tool: str = "ruff"
    paths: tuple[str, ...] = ("src", "tests")
    fix: bool = False
    check_format: bool = True
    extra_args: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.tool not in _TOOLS:
            raise ValueError(f"unsupported linter '{self.tool}'; expected one of {sorted(_TOOLS)}")

    def build_commands(self, ctx: ExecutionContext) -> list[str]:
        """Return lint commands appropriate to the selected tool."""
        if self.tool == "ruff":
            commands = [f"ruff check {' '.join(self.paths)}"]
            if self.check_format:
                commands.append("ruff format --check " + " ".join(self.paths))
            if self.fix:
                commands.append("ruff check --fix " + " ".join(self.paths))
            return [*commands, *self.extra_args]
        if self.tool == "eslint":
            glob_targets = " ".join(f"'{path}/**/*.{{js,ts}}'" for path in self.paths)
            fix_flag = "--fix" if self.fix else "--max-warnings=-1"
            return [f"npx eslint {glob_targets} {fix_flag}", *self.extra_args]
        # clippy: deny warnings so issues fail the build deterministically
        return ["cargo clippy --all-targets -- -D warnings", *self.extra_args]

    def post_process(self, ctx: ExecutionContext, result: StageResult) -> StageResult:
        """Extract total issue count reported by the linter."""
        result.metadata["tool"] = self.tool
        result.metadata["fix"] = self.fix
        total = sum(int(match.group(1)) for match in _ISSUE_COUNT.finditer(result.output))
        result.metadata["issues"] = total
        return result
