"""Shared pytest fixtures for the DevOpsPipeline test suite.

Fixtures provide fake executors (deterministic, cross-platform), a recording
runner, sample pipelines, a real LocalRunner bound to a temp workspace root,
and an encrypted vault backed by a password.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Optional

import pytest

from src.orchestrator.engine import PipelineEngine
from src.orchestrator.pipeline import CommandStage, Pipeline
from src.runners.base import JobResult, JobSpec, JobStatus, Runner
from src.security.vault import SecretVault

# --------------------------------------------------------------------------- #
# fake executor machinery
# --------------------------------------------------------------------------- #
FakeExecutor = Callable[[Any, Any, Any], Any]


@dataclass
class FakeRunnerSpec:
    """Record of one command batch seen by a fake executor."""

    commands: list[str]
    env: Optional[dict[str, str]]
    timeout: Optional[float]


def make_fake_executor(
    *,
    stdout: str = "",
    stderr: str = "",
    exit_code: int = 0,
    delay: float = 0.0,
) -> tuple[FakeExecutor, list[FakeRunnerSpec]]:
    """Build an executor returning canned output and recording invocations."""
    calls: list[FakeRunnerSpec] = []

    def execute(commands: Any, env: Any, timeout: Any) -> Any:
        calls.append(FakeRunnerSpec([str(c) for c in commands], dict(env) if env else None, timeout))
        if delay:
            time.sleep(delay)
        return SimpleNamespace(exit_code=exit_code, stdout=stdout, stderr=stderr)

    return execute, calls


@pytest.fixture()
def fake_executor() -> Callable[..., tuple[FakeExecutor, list[FakeRunnerSpec]]]:
    """Factory fixture so each test configures its own canned outcome."""
    return make_fake_executor


@pytest.fixture()
def execution_context(tmp_path: Path, fake_executor: Callable[..., Any]) -> Any:
    """ExecutionContext wired to a successful fake executor."""
    from src.orchestrator.stage import ExecutionContext

    executor, calls = make_fake_executor(stdout="ok\n")
    ctx = ExecutionContext(
        run_id="test-run",
        pipeline_name="sample",
        workspace=tmp_path / "ws",
        environment={"PIPELINE_ENV": "1"},
        variables={"branch": "main"},
        executor=executor,
    )
    (tmp_path / "ws").mkdir(parents=True, exist_ok=True)
    ctx.calls = calls  # type: ignore[attr-defined]
    return ctx


# --------------------------------------------------------------------------- #
# runners
# --------------------------------------------------------------------------- #
class RecordingRunner(Runner):
    """Runner that records specs and fails when commands match patterns."""

    name = "recording"

    def __init__(self, fail_patterns: tuple[str, ...] = ()) -> None:
        super().__init__()
        self.fail_patterns = fail_patterns
        self.seen: list[JobSpec] = []

    def execute(self, spec: JobSpec) -> JobResult:
        self.seen.append(spec)
        joined = spec.joined_commands
        failed = any(pattern in joined for pattern in self.fail_patterns)
        status = JobStatus.FAILED if failed else JobStatus.SUCCESS
        return JobResult(
            spec_id=spec.id,
            status=status,
            exit_code=1 if failed else 0,
            stdout=joined + "\n",
            stderr="boom" if failed else "",
        )


@pytest.fixture()
def recording_runner() -> RecordingRunner:
    return RecordingRunner()


@pytest.fixture()
def local_runner(tmp_path: Path) -> LocalRunnerType:
    from src.runners.local import LocalRunner

    return LocalRunner(workspace_root=tmp_path / "local-ws")


LocalRunnerType = Any


# --------------------------------------------------------------------------- #
# pipelines & engines
# --------------------------------------------------------------------------- #
@pytest.fixture()
def sample_pipeline() -> Pipeline:
    """Diamond-shaped pipeline: a -> (b, c) -> d."""
    return Pipeline(
        name="sample-diamond",
        description="fixture pipeline",
        stages=[
            CommandStage(name="a", commands=["echo a"]),
            CommandStage(name="b", commands=["echo b"], depends_on=("a",)),
            CommandStage(name="c", commands=["echo c"], depends_on=("a",)),
            CommandStage(name="d", commands=["echo d"], depends_on=("b", "c")),
        ],
        max_parallel=4,
    )


@pytest.fixture()
def engine(local_runner: Any, tmp_path: Path) -> PipelineEngine:
    """Engine over the local runner with a run-scoped workspace root."""
    return PipelineEngine(local_runner, max_parallel=2, workspace_root=tmp_path / "engine-ws")


# --------------------------------------------------------------------------- #
# security fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture()
def vault(tmp_path: Path) -> SecretVault:
    """Password-derived vault persisted into the test's tmp dir."""
    return SecretVault(password="correct-horse-battery-staple", store_path=tmp_path / "vault.json")


@pytest.fixture()
def rsa_keypair() -> dict[str, str]:
    """RSA PEM keypair generated via cryptography."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    public_pem = (
        private_key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode("ascii")
    )
    return {"private": private_pem, "public": public_pem}


@pytest.fixture()
def oidc_validator(rsa_keypair: dict[str, str]) -> Any:
    """Validator pinned to the fixture's public key (no network)."""
    from src.security.oidc import OIDCConfig, OIDCValidator

    config = OIDCConfig(
        issuer="https://auth.example.com",
        audience="devops-ci",
        public_key_pem=rsa_keypair["public"],
    )
    validator = OIDCValidator(config)
    validator._test_private_pem = rsa_keypair["private"]  # type: ignore[attr-defined]
    return validator


@dataclass
class ArtifactSeed:
    """Helper describing an artifact file planted on disk."""

    path: Path
    content: bytes = field(default=b"data")


@pytest.fixture()
def artifact_store(tmp_path: Path) -> Any:
    """Local artifact store plus one seeded blob."""
    from src.artifacts.store import LocalArtifactStore

    store = LocalArtifactStore(tmp_path / "artifacts")
    source_dir = tmp_path / "seed"
    source_dir.mkdir()
    (source_dir / "report.txt").write_bytes(b"coverage report body")
    store.put(source_dir / "report.txt", "run-1/report.txt", metadata={"stage": "test"})
    return store
