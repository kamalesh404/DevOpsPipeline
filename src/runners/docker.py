"""Docker runner: executes jobs inside ephemeral containers.

Container configuration is built by a pure static method
(:meth:`DockerRunner.build_container_config`) so it can be unit-tested without
the Docker SDK, which is imported lazily only when actually talking to a
daemon.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from src.runners.base import JobResult, JobSpec, JobStatus, Runner

LOG = logging.getLogger("devopspipeline.runner.docker")

CONTAINER_WORKSPACE = "/workspace"
RUNNER_LABEL = "devopspipeline"


class DockerRunner(Runner):
    """Run each job in a throwaway container with the workspace bind-mounted."""

    name = "docker"

    def __init__(
        self,
        workspace_root: str | Path | None = None,
        *,
        image: str = "python:3.12-slim",
        docker_host: str | None = None,
        auto_pull: bool = True,
        container_workspace: str = CONTAINER_WORKSPACE,
    ) -> None:
        super().__init__(workspace_root)
        self.image = image
        self.docker_host = docker_host
        self.auto_pull = auto_pull
        self.container_workspace = container_workspace
        self._client: Any = None

    def _get_client(self) -> Any:
        """Lazily construct (and cache) the Docker SDK client."""
        if self._client is None:
            import docker  # deferred: heavy optional dependency

            kwargs: dict[str, Any] = {}
            if self.docker_host:
                kwargs["environment"] = {"DOCKER_HOST": self.docker_host}
            client = docker.from_env(**kwargs)
            client.ping()
            self._client = client
        return self._client

    @staticmethod
    def build_container_config(
        spec: JobSpec,
        *,
        image: str,
        container_workspace: str = CONTAINER_WORKSPACE,
    ) -> dict[str, Any]:
        """Pure builder producing ``containers.run`` keyword arguments.

        Exposed for testing; performs no I/O and imports nothing external.
        """
        return {
            "image": image,
            "command": ["/bin/bash", "-lc", spec.joined_commands],
            "environment": {str(k): str(v) for k, v in (spec.env or {}).items()},
            "working_dir": container_workspace,
            "volumes": {
                str(Path(spec.workspace).resolve()): {
                    "bind": container_workspace,
                    "mode": "rw",
                }
            },
            "labels": {
                RUNNER_LABEL: "true",
                "devopspipeline.job": spec.id,
                **dict(spec.labels),
            },
            "detach": True,
            "stdout": True,
            "stderr": True,
            "tty": False,
        }

    def _ensure_image(self, client: Any, reference: str) -> None:
        """Pull ``reference`` when allowed and not present locally."""
        if not self.auto_pull:
            return
        try:
            client.images.get(reference)
            return
        except Exception:
            LOG.info("pulling image %s", reference)
        client.images.pull(reference)

    def execute(self, spec: JobSpec) -> JobResult:
        """Create, start, wait on, collect logs from, then remove one container."""
        client = self._get_client()
        image = spec.image or self.image
        config = self.build_container_config(
            spec, image=image, container_workspace=self.container_workspace
        )
        self.prepare_workspace(spec)
        self._ensure_image(client, image)

        started = time.time()
        result = JobResult(spec_id=spec.id, started_at=started)
        try:
            container = client.containers.run(**config)
        except Exception as exc:
            result.stderr = f"container start failed: {exc}"
            result.finished_at = time.time()
            result.duration = result.finished_at - started
            return result

        try:
            status_code: int | None = None
            try:
                outcome = container.wait(timeout=spec.timeout)
                status_code = int(outcome.get("StatusCode", -1))
            except TimeoutError:
                container.kill()
                result.status = JobStatus.TIMEOUT
                result.exit_code = 124
            except Exception as exc:  # network/daemon hiccups surface here
                if "timed out" in str(exc).lower():
                    container.kill()
                    result.status = JobStatus.TIMEOUT
                    result.exit_code = 124
                else:
                    raise
            if status_code is not None:
                result.exit_code = status_code
                result.status = JobStatus.SUCCESS if status_code == 0 else JobStatus.FAILED
            result.stdout = container.logs(stdout=True, stderr=False).decode(errors="replace")
            stderr_text = container.logs(stdout=False, stderr=True).decode(errors="replace")
            result.stderr = stderr_text
        except Exception as exc:
            result.status = JobStatus.FAILED
            result.stderr = f"{result.stderr}\nrunner error: {exc}".strip()
        finally:
            try:
                container.remove(force=True)
            except Exception:  # best effort cleanup
                LOG.warning("failed to remove container for job %s", spec.id)

        result.finished_at = time.time()
        result.duration = result.finished_at - started
        return result

    def healthcheck(self) -> bool:
        """Ping the Docker daemon."""
        try:
            self._get_client().ping()
            return True
        except Exception:
            return False

    def cleanup(self, workspace: Path) -> None:
        """Force-remove any containers left over from this workspace's runs."""
        try:
            client = self._get_client()
            leftovers = client.containers.list(
                all=True, filters={"label": f"{RUNNER_LABEL}=true"}
            )
            for container in leftovers:
                container.remove(force=True)
        except Exception:
            LOG.debug("cleanup sweep failed", exc_info=True)
