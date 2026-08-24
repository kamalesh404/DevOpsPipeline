"""Kubernetes runner: executes jobs as one-shot pods.

Pod manifests are produced by the pure :meth:`KubernetesRunner.build_pod_manifest`
static method (unit-testable without a cluster); the official Kubernetes
Python client is imported lazily only when creating/watching real pods.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from src.runners.base import JobResult, JobSpec, JobStatus, Runner

LOG = logging.getLogger("devopspipeline.runner.kubernetes")

POD_WORKSPACE = "/workspace"
TERMINAL_PHASES = {"Succeeded", "Failed"}


class KubernetesRunner(Runner):
    """Schedule each job as a Never-restart pod in a target namespace."""

    name = "kubernetes"

    def __init__(
        self,
        workspace_root: str | Path | None = None,
        *,
        namespace: str = "default",
        pod_image: str = "python:3.12-slim",
        kubeconfig: str | None = None,
        service_account: str | None = None,
        resources: dict[str, Any] | None = None,
        poll_interval: float = 2.0,
    ) -> None:
        super().__init__(workspace_root)
        self.namespace = namespace
        self.pod_image = pod_image
        self.kubeconfig = kubeconfig
        self.service_account = service_account
        self.resources = resources or {
            "requests": {"cpu": "100m", "memory": "128Mi"},
            "limits": {"memory": "512Mi"},
        }
        self.poll_interval = poll_interval
        self._core_v1: Any = None

    def _get_api(self) -> Any:
        """Lazily load kubeconfig/in-cluster config and return CoreV1Api."""
        if self._core_v1 is None:
            from kubernetes import client, config  # deferred optional dependency

            try:
                config.load_incluster_config()
            except config.ConfigException:
                config.load_kube_config(config_file=self.kubeconfig)
            self._core_v1 = client.CoreV1Api()
        return self._core_v1

    @staticmethod
    def build_pod_manifest(
        spec: JobSpec,
        *,
        namespace: str,
        image: str,
        container_workspace: str = POD_WORKSPACE,
        service_account: str | None = None,
        resources: dict[str, Any] | None = None,
        labels: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Pure builder returning a V1Pod-compatible manifest dictionary."""
        manifest_labels = {"app": "devopspipeline", "job-id": spec.id, **(labels or {})}
        container: dict[str, Any] = {
            "name": "main",
            "image": image,
            "command": ["/bin/sh", "-lc", spec.joined_commands],
            "workingDir": container_workspace,
            "volumeMounts": [
                {"name": "workspace", "mountPath": container_workspace}
            ],
            "env": [
                {"name": str(key), "value": str(value)}
                for key, value in (spec.env or {}).items()
            ],
        }
        if resources:
            container["resources"] = resources
        manifest: dict[str, Any] = {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": f"devops-job-{spec.id}",
                "namespace": namespace,
                "labels": manifest_labels,
            },
            "spec": {
                "restartPolicy": "Never",
                "containers": [container],
                "volumes": [{"name": "workspace", "emptyDir": {}}],
                "ttlSecondsAfterFinished": 3600,
            },
        }
        if service_account:
            manifest["spec"]["serviceAccountName"] = service_account
        return manifest

    @staticmethod
    def phase_to_status(phase: str) -> JobStatus:
        """Map a pod phase onto runner job status."""
        if phase == "Succeeded":
            return JobStatus.SUCCESS
        if phase == "Failed":
            return JobStatus.FAILED
        return JobStatus.RUNNING

    def execute(self, spec: JobSpec) -> JobResult:
        """Create the pod, watch it to completion, then fetch logs."""
        api = self._get_api()
        manifest = self.build_pod_manifest(
            spec,
            namespace=self.namespace,
            image=spec.image or self.pod_image,
            service_account=self.service_account,
            resources=self.resources,
        )
        name: str = manifest["metadata"]["name"]
        started = time.time()
        result = JobResult(spec_id=spec.id, started_at=started)

        try:
            api.create_namespaced_pod(namespace=self.namespace, body=manifest)
            LOG.info("[%s] pod %s created in %s", spec.id, name, self.namespace)
            deadline = time.monotonic() + (spec.timeout or 600.0)
            phase = ""
            while time.monotonic() < deadline:
                status = api.read_namespaced_pod_status(name, namespace=self.namespace)
                phase = str(status.status.phase)
                if phase in TERMINAL_PHASES:
                    break
                time.sleep(self.poll_interval)

            if phase not in TERMINAL_PHASES:
                result.status = JobStatus.TIMEOUT
                result.exit_code = 124
            else:
                result.status = self.phase_to_status(phase)
                result.exit_code = 0 if result.status is JobStatus.SUCCESS else 1
            try:
                result.stdout = api.read_namespaced_pod_log(
                    name, namespace=self.namespace
                )
            except Exception:
                LOG.warning("[%s] could not read logs for pod %s", spec.id, name)
        except Exception as exc:
            result.status = JobStatus.FAILED
            result.stderr = f"kubernetes error: {exc}"
        finally:
            try:
                api.delete_namespaced_pod(name, namespace=self.namespace)
            except Exception:
                LOG.debug("pod %s already gone", name)

        result.finished_at = time.time()
        result.duration = result.finished_at - started
        return result

    def healthcheck(self) -> bool:
        """Verify API server connectivity by listing nodes."""
        try:
            self._get_api().list_node(limit=1)
            return True
        except Exception:
            return False
