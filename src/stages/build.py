"""Build stage supporting make, npm, cargo and docker builds.

The stage translates a typed configuration into concrete shell commands,
validates the selected build system, and collects declared artifact globs
from the workspace on success so later stages (or the artifact store) can
consume them.
"""

from __future__ import annotations

import glob as globlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from src.orchestrator.stage import BaseStage, ExecutionContext, StageResult

BUILD_SYSTEMS = frozenset({"make", "npm", "cargo", "docker"})
MAX_ARTIFACTS = 50


@dataclass
class BuildStage(BaseStage):
    """Compile/package a project with the requested build system."""

    system: str = "make"
    target: Optional[str] = None
    flags: tuple[str, ...] = ()
    dockerfile: str = "Dockerfile"
    image_tag: Optional[str] = None
    use_cache: bool = True
    artifact_globs: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.system not in BUILD_SYSTEMS:
            raise ValueError(f"unsupported build system '{self.system}'; expected one of {sorted(BUILD_SYSTEMS)}")
        if self.system == "docker" and not self.image_tag:
            self.image_tag = f"{self.name.replace('build', 'app').strip() or 'app'}:latest"

    def _npm_commands(self) -> list[str]:
        """Commands for Node.js projects using npm."""
        return [
            "npm ci",
            f"npm run build {' '.join(self.flags)}".rstrip(),
        ]

    def _cargo_commands(self) -> list[str]:
        """Commands for Rust projects using cargo."""
        base = ["cargo", "build", "--release", *self.flags]
        if self.target:
            base.extend(["--bin", self.target])
        return [" ".join(base)]

    def _docker_commands(self) -> list[str]:
        """Commands for container image builds."""
        assert self.image_tag is not None  # narrowed by __post_init__
        cache_flag = "" if self.use_cache else "--no-cache"
        return [
            f"docker build {cache_flag} -f {self.dockerfile} -t {self.image_tag} .".replace("  ", " ")
        ]

    def _make_commands(self) -> list[str]:
        """Commands for Makefile-driven projects."""
        parts = ["make"]
        if self.target:
            parts.append(self.target)
        parts.extend(self.flags)
        return [" ".join(parts)]

    def build_commands(self, ctx: ExecutionContext) -> list[str]:
        """Dispatch to the per-system command builder."""
        builders = {
            "make": self._make_commands,
            "npm": self._npm_commands,
            "cargo": self._cargo_commands,
            "docker": self._docker_commands,
        }
        return builders[self.system]()

    def post_process(self, ctx: ExecutionContext, result: StageResult) -> StageResult:
        """Collect artifacts matching configured globs from the workspace."""
        result.metadata["system"] = self.system
        if self.image_tag:
            result.metadata["image"] = self.image_tag
        if result.succeeded and self.artifact_globs:
            collected: list[str] = []
            for pattern in self.artifact_globs:
                matches = globlib.glob(str(Path(ctx.workspace) / pattern), recursive=True)
                collected.extend(
                    path for path in sorted(matches)[:MAX_ARTIFACTS] if Path(path).is_file()
                )
            result.artifacts = [str(Path(p).relative_to(ctx.workspace)) for p in collected[:MAX_ARTIFACTS]]
        return result
