"""Test stage for pytest, jest and cargo test.

Runs the configured framework, parses pass/fail counts and coverage
percentage from output, enforces optional coverage thresholds, and collects
JUnit XML reports as artifacts when requested.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from src.orchestrator.stage import BaseStage, ExecutionContext, StageResult

_FRAMEWORKS = frozenset({"pytest", "jest", "cargo"})
_COVERAGE_PATTERNS = (
    re.compile(r"(?:TOTAL|All files).*?(\d+(?:\.\d+)?)%", re.MULTILINE),
    re.compile(r"[Cc]overage[: ]+(\d+(?:\.\d+)?)%"),
)
_PASSED_PATTERN = re.compile(r"(\d+)\s+passed")
_FAILED_PATTERN = re.compile(r"(\d+)\s+failed")


@dataclass
class TestStage(BaseStage):
    """Execute a test suite with optional coverage enforcement."""

    framework: str = "pytest"
    paths: tuple[str, ...] = ()
    coverage: bool = False
    coverage_min: Optional[float] = None
    junit_report: Optional[str] = None
    markers: Optional[str] = None

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.framework not in _FRAMEWORKS:
            raise ValueError(f"unsupported framework '{self.framework}'; expected one of {sorted(_FRAMEWORKS)}")
        if self.coverage_min is not None and not 0.0 <= self.coverage_min <= 100.0:
            raise ValueError("coverage_min must be between 0 and 100")

    def build_commands(self, ctx: ExecutionContext) -> list[str]:
        """Return the framework's test invocation."""
        if self.framework == "pytest":
            command = ["python -m pytest"]
            command.extend(self.paths)
            command.append("-v")
            if self.junit_report:
                command.append(f"--junitxml={self.junit_report}")
            if self.markers:
                command.append(f"-m '{self.markers}'")
            if self.coverage or self.coverage_min is not None:
                command.extend(["--cov=src", "--cov-report=term"])
            return [" ".join(command)]
        if self.framework == "jest":
            flags = ["--ci"]
            if self.coverage or self.coverage_min is not None:
                flags.append("--coverage")
            return ["npx jest " + " ".join([*self.paths, *flags])]
        return ["cargo test --release"]

    def post_process(self, ctx: ExecutionContext, result: StageResult) -> StageResult:
        """Parse metrics from output; fail when below ``coverage_min``."""
        result.metadata["framework"] = self.framework

        passed_match = _PASSED_PATTERN.search(result.output)
        failed_match = _FAILED_PATTERN.search(result.output)
        result.metadata["passed"] = int(passed_match.group(1)) if passed_match else None
        result.metadata["failed"] = int(failed_match.group(1)) if failed_match else None

        coverage: Optional[float] = None
        for pattern in _COVERAGE_PATTERNS:
            match = pattern.search(result.output)
            if match:
                coverage = float(match.group(1))
                break
        result.metadata["coverage"] = coverage

        if (
            result.succeeded
            and self.coverage_min is not None
            and (coverage is None or coverage < self.coverage_min)
        ):
            observed = "unknown" if coverage is None else f"{coverage:.1f}%"
            result.status = StageStatus.FAILED
            result.error = f"coverage {observed} is below required minimum {self.coverage_min}%"

        if self.junit_report and result.succeeded:
            report = Path(ctx.workspace) / self.junit_report
            if report.exists():
                result.artifacts.append(self.junit_report)
        return result
