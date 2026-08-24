"""Git checkout stage.

Clones the target repository into the run workspace with shallow-fetch
optimizations, optional submodule bootstrap, and pull-request head checkout.
Parses the resolved commit SHA into stage metadata for downstream reporting.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from src.orchestrator.stage import BaseStage, ExecutionContext, RetryPolicy, StageResult

_SHA_PATTERN = re.compile(r"\b([0-9a-f]{40})\b", re.MULTILINE)


@dataclass
class CheckoutStage(BaseStage):
    """Clone/fetch a repository and place it at the workspace root."""

    repo_url: str = ""
    ref: str = "main"
    depth: int = 1
    submodules: bool = False
    use_ssh: bool = False
    pr_number: Optional[int] = None

    def __post_init__(self) -> None:
        super().__post_init__()
        if not self.repo_url:
            raise ValueError("CheckoutStage requires repo_url")
        if self.depth < 1:
            raise ValueError("depth must be >= 1")

    @property
    def effective_url(self) -> str:
        """Repository URL, optionally rewritten for SSH remotes."""
        if self.use_ssh and self.repo_url.startswith("https://github.com/"):
            return self.repo_url.replace("https://github.com/", "git@github.com:")
        return self.repo_url

    def build_commands(self, ctx: ExecutionContext) -> list[str]:
        """Compose clone + fetch + checkout commands for this job."""
        commands: list[str] = []
        branch_flag = f" --branch {self.ref}" if self.pr_number is None else ""
        commands.append(
            f"git clone --depth {self.depth}{branch_flag} {self.effective_url} ."
        )
        if self.pr_number is not None:
            commands.append(
                f"git fetch origin pull/{self.pr_number}/head:pr-{self.pr_number} --depth {self.depth}"
            )
            commands.append(f"git checkout pr-{self.pr_number}")
        elif self.ref not in ("HEAD", "main"):
            # Branch was cloned above; keep FETCH_HEAD fresh for exact refs.
            commands.append(f"git fetch origin {self.ref} --depth {self.depth}")
        if self.submodules:
            commands.append(
                f"git submodule update --init --recursive --depth {self.depth}"
            )
        commands.append("git rev-parse HEAD")
        return commands

    def post_process(self, ctx: ExecutionContext, result: StageResult) -> StageResult:
        """Extract the resolved commit SHA from command output."""
        match = _SHA_PATTERN.search(result.output)
        if match:
            result.metadata["commit"] = match.group(1)
        result.metadata["ref"] = self.ref
        result.metadata["pr_number"] = self.pr_number
        return result

    @classmethod
    def for_pull_request(
        cls,
        repo_url: str,
        pr_number: int,
        **kwargs: object,
    ) -> "CheckoutStage":
        """Convenience constructor targeting a PR merge-head."""
        kwargs.setdefault("name", f"checkout-pr-{pr_number}")  # type: ignore[arg-type]
        return cls(repo_url=repo_url, pr_number=pr_number, **kwargs)  # type: ignore[arg-type]
