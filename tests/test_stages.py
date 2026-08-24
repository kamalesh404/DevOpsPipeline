"""Tests for built-in stage implementations."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.orchestrator.stage import ExecutionContext, StageResult, StageStatus
from src.stages.build import BuildStage
from src.stages.checkout import CheckoutStage
from src.stages.deploy import DeployStage
from src.stages.lint import LintStage
from src.stages.notify import NotifyStage
from src.stages.test import TestStage


# --------------------------------------------------------------------------- #
# build
# --------------------------------------------------------------------------- #
def test_build_stage_npm_commands() -> None:
    stage = BuildStage(name="build-js", system="npm", flags=("--verbose",))
    commands = stage.build_commands(None)  # type: ignore[arg-type]
    assert "npm ci" in commands
    assert any("npm run build --verbose" in command for command in commands)


def test_build_stage_docker_image_tag() -> None:
    stage = BuildStage(name="build-image", system="docker", image_tag="app:1.2.3")
    commands = stage.build_commands(None)  # type: ignore[arg-type]
    assert any("-t app:1.2.3" in command for command in commands)
    assert any("--no-cache" not in command for command in commands)


def test_build_stage_rejects_unknown_system() -> None:
    with pytest.raises(ValueError):
        BuildStage(name="bad", system="bazel")


def test_build_stage_collects_artifacts(execution_context: object, fake_executor: object) -> None:
    workspace: Path = execution_context.workspace
    (workspace / "dist").mkdir(exist_ok=True)
    (workspace / "dist" / "bundle.js").write_text("console.log('hi')")
    executor, _calls = fake_executor(stdout="built ok")

    from types import SimpleNamespace

    execution_context.executor = lambda c, e, t: SimpleNamespace(exit_code=0, stdout="ok", stderr="")
    stage = BuildStage(name="build", system="npm", artifact_globs=("dist/*",))
    result = stage.run(execution_context)
    assert result.succeeded
    assert result.artifacts == ["dist/bundle.js"]
    assert result.metadata["system"] == "npm"


# --------------------------------------------------------------------------- #
# test / lint
# --------------------------------------------------------------------------- #
def test_test_stage_parses_coverage_and_enforces_minimum(fake_executor: object, execution_context: object) -> None:
    from types import SimpleNamespace

    output = "12 passed\nTOTAL                                        87%"
    execution_context.executor = lambda c, e, t: SimpleNamespace(exit_code=0, stdout=output, stderr="")

    passing = TestStage(name="t", framework="pytest", coverage_min=80.0)
    failing = TestStage(name="t", framework="pytest", coverage_min=95.0)

    assert passing.run(execution_context).succeeded
    strict_result = failing.run(execution_context)
    assert strict_result.status is StageStatus.FAILED
    assert "coverage" in strict_result.error.lower()


def test_test_stage_pytest_command_shape() -> None:
    stage = TestStage(name="unit", framework="pytest", junit_report="junit.xml", markers="not slow")
    command = stage.build_commands(None)[0]  # type: ignore[arg-type]
    assert "--junitxml=junit.xml" in command
    assert "-m 'not slow'" in command


def test_lint_stage_ruff_with_fix() -> None:
    stage = LintStage(name="lint", tool="ruff", fix=True)
    joined = "\n".join(stage.build_commands(None))  # type: ignore[arg-type]
    assert "--fix" in joined
    assert "ruff format --check" in joined


def test_lint_stage_counts_issues(fake_executor: object) -> None:
    from types import SimpleNamespace

    executor, _calls = fake_executor(stdout="Found 3 errors.\n1 warning.")
    stage = LintStage(name="lint", tool="eslint")
    ctx = ExecutionContext(run_id="r", pipeline_name="p", workspace=".", executor=None)
    ctx.executor = executor
    result = stage.run(ctx)
    assert result.metadata["issues"] == 4


# --------------------------------------------------------------------------- #
# checkout / deploy
# --------------------------------------------------------------------------- #
def test_checkout_stage_clone_commands() -> None:
    stage = CheckoutStage(name="checkout", repo_url="https://github.com/acme/app.git", ref="develop")
    commands = stage.build_commands(None)  # type: ignore[arg-type]
    assert any(command.startswith("git clone") and "--branch develop" in command for command in commands)
    assert commands[-1] == "git rev-parse HEAD"


def test_checkout_pr_checkout_path() -> None:
    stage = CheckoutStage.for_pull_request("https://github.com/acme/app.git", pr_number=42)
    joined = "\n".join(stage.build_commands(None))  # type: ignore[arg-type]
    assert "pull/42/head" in joined
    assert stage.name == "checkout-pr-42"


def test_deploy_stage_dry_run_prefixes_commands() -> None:
    stage = DeployStage(name="deploy", target="aws", bucket="my-bucket", dry_run=True)
    commands = stage.build_commands(None)  # type: ignore[arg-type]
    assert all(command.startswith("echo [DRY-RUN]") for command in commands)


def test_deploy_production_requires_approval(execution_context: object) -> None:
    stage = DeployStage(
        name="ship-it",
        target="gcp",
        deploy_environment="production",
        image="gcr.io/p/a:1.0",
        approval_required=True,
    )
    skipped = stage.run(execution_context)
    assert skipped.status is StageStatus.SKIPPED
    execution_context.variables["approved"] = True
    approved = stage.run(execution_context)
    assert approved.status is StageStatus.SUCCESS  # fake executor + dry-run echo
    assert approved.metadata["environment"] == "production"
    assert "approval" not in approved.error


# --------------------------------------------------------------------------- #
# notify
# --------------------------------------------------------------------------- #
def test_notify_renders_template_variables(execution_context: object) -> None:
    execution_context.variables.update({"status": "SUCCESS"})
    stage = NotifyStage(
        name="notify",
        channels=("webhook",),
        webhook_url="https://hooks.invalid/tld",
        message_template="{pipeline} #{run_id} -> {status}",
    )
    assert stage._render(execution_context) == "sample #test-run -> SUCCESS"


def test_notify_delivers_payload(monkeypatch: object, execution_context: object) -> None:
    sent: list[tuple[str, dict]] = []

    def fake_post(url: str, payload: dict, timeout: float) -> bytes:
        sent.append((url, payload))
        return b"{}"

    monkeypatch.setattr(NotifyStage, "_http_post_json", staticmethod(fake_post))
    stage = NotifyStage(
        name="notify",
        channels=("slack",),
        webhook_url="https://hooks.example/abc",
        message_template="run done",
    )
    result = stage.run(execution_context)
    assert result.succeeded
    url, payload = sent[0]
    assert url.endswith("/abc")
    assert payload["text"] == "run done"
    assert payload["blocks"][0]["type"] == "header"


def test_notify_reports_delivery_failure(monkeypatch: object, execution_context: object) -> None:
    def broken_post(url: str, payload: dict, timeout: float) -> bytes:
        raise OSError("network unreachable")

    monkeypatch.setattr(NotifyStage, "_http_post_json", staticmethod(broken_post))
    stage = NotifyStage(name="notify", channels=("webhook",), webhook_url="https://x.invalid")
    result = stage.run(execution_context)
    assert result.status is StageStatus.FAILED
    assert "network unreachable" in result.error


def test_stage_result_serialization_roundtrip() -> None:
    result = StageResult(name="s", status=StageStatus.SUCCESS, exit_code=0, duration=1.5)
    data = result.to_dict()
    assert data["status"] == "SUCCESS"
