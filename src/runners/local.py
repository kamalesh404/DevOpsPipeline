"""Local process runner.

Executes job commands as child processes of the agent using the platform
shell. Tracks active processes so long-running jobs can be cancelled, and can
optionally purge per-run workspaces when the run finishes.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from src.runners.base import JobResult, JobSpec, JobStatus, Runner

LOG = logging.getLogger("devopspipeline.runner.local")


class LocalRunner(Runner):
    """Run jobs directly on the host machine."""

    name = "local"

    def __init__(
        self,
        workspace_root: str | Path | None = None,
        *,
        keep_workspaces: bool = False,
        shell: str | None = None,
    ) -> None:
        super().__init__(workspace_root)
        self.keep_workspaces = keep_workspaces
        self._shell = shell or self._default_shell()
        self._active: dict[int, subprocess.Popen[str]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _default_shell() -> str:
        """Pick a platform-appropriate shell executable path."""
        if sys.platform == "win32":
            return str(Path(os.environ.get("COMSPEC", "cmd.exe")))
        return shutil.which("bash") or "/bin/bash"

    @property
    def is_windows(self) -> bool:
        """Whether this runner executes through cmd.exe."""
        return sys.platform == "win32"

    def _build_argv(self, joined: str) -> list[str]:
        """Wrap the command string for the platform shell."""
        if self.is_windows:
            return ["cmd.exe", "/d", "/s", "/c", joined]
        return [self._shell, "-lc", joined]

    def execute(self, spec: JobSpec) -> JobResult:
        """Synchronously execute ``spec`` and capture output."""
        workspace = self.prepare_workspace(spec)
        argv = self._build_argv(spec.joined_commands)
        env = {
            **os.environ,
            **{str(key): str(value) for key, value in (spec.env or {}).items()},
        }
        started = time.time()
        result = JobResult(spec_id=spec.id, started_at=started)

        LOG.info("[%s] exec: %s (cwd=%s)", spec.id, spec.joined_commands[:120], workspace)
        try:
            process: subprocess.Popen[str] = subprocess.Popen(  # noqa: S603 - controlled input
                argv,
                cwd=str(workspace),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except OSError as exc:
            result.finished_at = time.time()
            result.stderr = f"failed to spawn process: {exc}"
            result.duration = time.time() - started
            return result

        with self._lock:
            self._active[process.pid] = process
        try:
            try:
                stdout, stderr = process.communicate(timeout=spec.timeout)
                result.exit_code = int(process.returncode or 0)
                result.status = JobStatus.SUCCESS if result.exit_code == 0 else JobStatus.FAILED
            except subprocess.TimeoutExpired:
                self._terminate(process)
                stdout, stderr = process.communicate()
                result.exit_code = 124
                result.status = JobStatus.TIMEOUT
                stderr = (stderr or "") + f"\njob exceeded timeout of {spec.timeout}s"
        finally:
            with self._lock:
                self._active.pop(process.pid, None)

        result.stdout = stdout or ""
        result.stderr = stderr or ""
        result.finished_at = time.time()
        result.duration = result.finished_at - started
        return result

    def _terminate(self, process: subprocess.Popen[str]) -> None:
        """Kill a timed-out process including its children."""
        if self.is_windows:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                capture_output=True,
                check=False,
            )
        else:
            process.kill()
        process.poll()

    def cancel_all(self) -> int:
        """Terminate every active job; returns how many were killed."""
        with self._lock:
            running = list(self._active.values())
        for process in running:
            self._terminate(process)
        return len(running)

    def healthcheck(self) -> bool:
        """Verify the configured shell exists on this machine."""
        if self.is_windows:
            return Path(self._shell).exists()
        return shutil.which(self._shell) is not None

    def cleanup(self, workspace: Path) -> None:
        """Remove the run workspace unless retention is requested."""
        if self.keep_workspaces:
            return
        resolved = Path(workspace).resolve()
        if resolved == Path(self.workspace_root).resolve():
            return
        if self.workspace_root not in resolved.parents and resolved != self.workspace_root:
            LOG.warning("refusing to clean workspace outside root: %s", resolved)
            return
        shutil.rmtree(resolved, ignore_errors=True)

    def describe(self) -> dict[str, object]:
        """Extend base metadata with runner-specific fields."""
        info = super().describe()
        info.update({"shell": self._shell, "keep_workspaces": self.keep_workspaces})
        return info
