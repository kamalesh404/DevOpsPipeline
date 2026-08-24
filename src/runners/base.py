"""Runner abstraction shared by local, Docker and Kubernetes executors.

A runner receives an immutable-ish :class:`JobSpec` describing the commands,
environment, workspace and timeout for one unit of work, executes it, and
returns a :class:`JobResult`. The orchestrator engine binds runners into stage
executors; stages never talk to runners directly.
"""

from __future__ import annotations

import shutil
import tempfile
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Optional


class JobStatus(str, Enum):
    """Terminal/intermediate states reported by runners."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    TIMEOUT = "TIMEOUT"
    CANCELLED = "CANCELLED"

    @property
    def ok(self) -> bool:
        """True only for successful jobs."""
        return self is JobStatus.SUCCESS


@dataclass(frozen=True)
class JobSpec:
    """Description of one executable job handed to a runner."""

    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    commands: tuple[str, ...] = ()
    env: Optional[Mapping[str, Any]] = None
    workspace: str | Path = "."
    timeout: Optional[float] = 600.0
    image: Optional[str] = None
    labels: Mapping[str, str] = field(default_factory=dict)

    @property
    def joined_commands(self) -> str:
        """Commands flattened into one shell string joined with '&&'."""
        return " && ".join(command.strip() for command in self.commands if command.strip())

    def to_dict(self) -> dict[str, Any]:
        """Serialize for logging/inspection (environment redacted)."""
        return {
            "id": self.id,
            "commands": list(self.commands),
            "workspace": str(self.workspace),
            "timeout": self.timeout,
            "image": self.image,
            "labels": dict(self.labels),
            "env_keys": sorted((self.env or {}).keys()),
        }


@dataclass
class JobResult:
    """Outcome of executing one job."""

    spec_id: str
    status: JobStatus = JobStatus.FAILED
    exit_code: int = -1
    stdout: str = ""
    stderr: str = ""
    duration: float = 0.0
    started_at: Optional[float] = None
    finished_at: Optional[float] = None

    @property
    def succeeded(self) -> bool:
        """Whether the job exited successfully."""
        return self.status is JobStatus.SUCCESS

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-friendly dictionary."""
        return {
            "spec_id": self.spec_id,
            "status": self.status.value,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "duration": round(self.duration, 4),
        }


class Runner(ABC):
    """Abstract execution backend.

    Concrete runners implement :meth:`execute`; optional hooks cover
    health probing, per-workspace cleanup and capability description.
    """

    name: str = "base"

    def __init__(self, workspace_root: str | Path | None = None) -> None:
        default_root = Path(tempfile.gettempdir()) / "devopspipeline" / self.name
        self.workspace_root = Path(workspace_root) if workspace_root else default_root
        self.workspace_root.mkdir(parents=True, exist_ok=True)

    @abstractmethod
    def execute(self, spec: JobSpec) -> JobResult:
        """Run ``spec`` synchronously and return its result."""

    def healthcheck(self) -> bool:
        """Cheap probe used by dashboards and pre-flight checks."""
        return True

    def prepare_workspace(self, spec: JobSpec) -> Path:
        """Ensure the job's workspace directory exists and return it."""
        workspace = Path(spec.workspace)
        workspace.mkdir(parents=True, exist_ok=True)
        return workspace

    def cleanup(self, workspace: Path) -> None:
        """Optional post-run cleanup hook (default: no-op)."""
        return None

    @staticmethod
    def resolve_binary(binary: str) -> Optional[str]:
        """Locate an executable on PATH, returning None when missing."""
        return shutil.which(binary)

    def describe(self) -> dict[str, Any]:
        """Metadata shown in dashboards and `runner list` style output."""
        return {
            "name": self.name,
            "type": type(self).__name__,
            "workspace_root": str(self.workspace_root),
            "healthy": self.healthcheck(),
        }
