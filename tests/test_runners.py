"""Tests for runner implementations (local execution + manifest builders)."""

from __future__ import annotations

import sys

import pytest

from src.runners.base import JobResult, JobSpec, JobStatus
from src.runners.docker import DockerRunner
from src.runners.kubernetes import KubernetesRunner
from tests.conftest import RecordingRunner


def test_local_runner_success(local_runner: object) -> None:
    spec = JobSpec(commands=["python -V"], workspace=".", timeout=30.0)
    result = local_runner.execute(spec)
    assert result.status is JobStatus.SUCCESS
    assert result.exit_code == 0


def test_local_runner_failure_propagates_exit_code(local_runner: object) -> None:
    spec = JobSpec(commands=["python -c \"import sys; sys.exit(2)\""], workspace=".")
    result = local_runner.execute(spec)
    assert result.status is JobStatus.FAILED
    assert result.exit_code == 2


def test_local_runner_env_passthrough(local_runner: object, tmp_path: object) -> None:
    command = "python -c \"import os; print(os.environ.get('DOP_TEST_VAR', 'missing'))\""
    spec = JobSpec(
        commands=[command],
        env={"DOP_TEST_VAR": "hello-runner"},
        workspace=str(tmp_path),
        timeout=60.0,
    )
    result = local_runner.execute(spec)
    assert result.status is JobStatus.SUCCESS
    assert "hello-runner" in result.stdout


def test_local_runner_timeout_marks_job(local_runner: object) -> None:
    if sys.platform == "win32":  # taskkill tree latency varies on CI runners
        pytest.skip("flaky process-tree teardown timing on Windows")
    spec = JobSpec(
        commands=["python -c \"import time; time.sleep(30)\""],
        timeout=1.5,
    )
    result = local_runner.execute(spec)
    assert result.status is JobStatus.TIMEOUT
    assert result.exit_code != 0


def test_recording_runner_tracks_specs() -> None:
    runner = RecordingRunner(fail_patterns=("bad",))
    good = JobSpec(id="g1", commands=["echo fine"])
    bad = JobSpec(id="b1", commands=["do bad things"])
    assert runner.execute(good).succeeded
    bad_result: JobResult = runner.execute(bad)
    assert not bad_result.succeeded
    assert len(runner.seen) == 2


def test_job_spec_joins_commands() -> None:
    spec = JobSpec(commands=["step one", " step two "])
    assert spec.joined_commands == "step one && step two"


def test_docker_container_config_is_pure(tmp_path: object) -> None:
    spec = JobSpec(id="dock1", commands=["make test"], env={"CI": "true"}, workspace=str(tmp_path))
    config = DockerRunner.build_container_config(spec, image="python:3.12-slim")
    assert config["image"] == "python:3.12-slim"
    assert config["command"][-1] == "make test"
    assert config["environment"]["CI"] == "true"
    assert str(tmp_path) in config["volumes"]
    assert config["labels"]["devopspipeline.job"] == "dock1"


def test_kubernetes_pod_manifest_shape(tmp_path: object) -> None:
    spec = JobSpec(id="k8s1", commands=["pytest -q"], env={"NODES": "2"})
    manifest = KubernetesRunner.build_pod_manifest(
        spec,
        namespace="ci",
        image="python:3.12-slim",
        service_account="pipeline-sa",
        resources={"requests": {"cpu": "250m"}},
    )
    pod_spec = manifest["spec"]
    container = pod_spec["containers"][0]
    assert manifest["metadata"]["namespace"] == "ci"
    assert manifest["metadata"]["name"] == "devops-job-k8s1"
    assert pod_spec["restartPolicy"] == "Never"
    assert pod_spec["serviceAccountName"] == "pipeline-sa"
    assert container["command"][0] == "/bin/sh"
    assert {"name": "NODES", "value": "2"} in container["env"]
    assert container["resources"] == {"requests": {"cpu": "250m"}}


def test_kubernetes_phase_mapping() -> None:
    assert KubernetesRunner.phase_to_status("Succeeded") is JobStatus.SUCCESS
    assert KubernetesRunner.phase_to_status("Failed") is JobStatus.FAILED
    assert KubernetesRunner.phase_to_status("Running") is JobStatus.RUNNING


def test_job_result_serialization() -> None:
    result = JobResult(spec_id="x", status=JobStatus.SUCCESS, exit_code=0, stdout="hi", duration=1.25)
    payload = result.to_dict()
    assert payload["status"] == "SUCCESS"
    assert payload["duration"] == 1.25


def test_runner_describe_reports_health(recording_runner: object) -> None:
    info = recording_runner.describe()
    assert info["type"] == "RecordingRunner"
    assert "workspace_root" in info
