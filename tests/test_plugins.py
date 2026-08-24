"""Tests for the plugin system and integrations."""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest

from src.plugins.base import Plugin, PluginError, PluginManager, WebhookAuthError
from src.plugins.docker_registry import RegistryClient, parse_image_ref
from src.plugins.github import GitHubClient, GitHubPlugin, verify_webhook_signature
from src.plugins.gitlab import verify_webhook_token
from src.plugins.slack import SlackPlugin, fields_block, header, section


# --------------------------------------------------------------------------- #
# manager & hooks
# --------------------------------------------------------------------------- #
class Recorder(Plugin):
    """Test double capturing hook invocations."""

    def __init__(self, name: str, *, explode: bool = False) -> None:
        super().__init__(name=name)
        self.explode = explode
        self.calls: list[str] = []

    def on_pipeline_start(self, run: object) -> None:
        self.calls.append("start")
        if self.explode:
            raise RuntimeError("plugin bug")

    def on_pipeline_complete(self, run: object) -> None:
        self.calls.append("complete")


def test_register_and_lookup() -> None:
    manager = PluginManager()
    plugin = Recorder("recorder")
    manager.register(plugin)
    assert manager.get("recorder") is plugin
    assert "recorder" in manager.names


def test_duplicate_registration_rejected() -> None:
    manager = PluginManager()
    manager.register(Recorder("dupe"))
    with pytest.raises(PluginError):
        manager.register(Recorder("dupe"))
    # replace=True is allowed
    manager.register(Recorder("dupe"), replace=True)


def test_disabled_plugins_do_not_receive_events() -> None:
    manager = PluginManager()
    plugin = Recorder("quiet")
    manager.register(plugin)
    manager.disable("quiet")
    assert manager.fire_pipeline_start({"id": "r1"}) == 0
    manager.enable("quiet")
    assert manager.fire_pipeline_start({"id": "r2"}) == 1
    assert plugin.calls == ["start"]


def test_hook_exceptions_are_isolated(caplog: object) -> None:
    manager = PluginManager()
    broken = Recorder("broken", explode=True)
    healthy = Recorder("healthy")
    manager.register(broken)
    manager.register(healthy)
    delivered = manager.fire_pipeline_start({})
    assert delivered == 1  # healthy plugin still notified
    assert healthy.calls == ["start"]


def test_webhook_dispatch_routes_to_named_plugin() -> None:
    class Echoer(Plugin):
        def handle_webhook(self, event_type: str, payload: object, headers: object) -> dict:
            return {"got": event_type}

    manager = PluginManager()
    manager.register(Echoer("github"))
    manager.register(Echoer("slack"))
    responses = manager.dispatch_webhook("github", "push", {}, {})
    assert responses == [{"got": "push"}]


# --------------------------------------------------------------------------- #
# github
# --------------------------------------------------------------------------- #
def test_github_webhook_signature_verification() -> None:
    secret = "hush"
    body = json.dumps({"action": "opened"}).encode()
    good = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert verify_webhook_signature(body, good, secret)
    assert not verify_webhook_signature(body, "sha256=deadbeef", secret)
    assert not verify_webhook_signature(body, "md5=nope", secret)


def test_github_parse_pull_request_event() -> None:
    payload = {
        "action": "synchronize",
        "repository": {"full_name": "acme/app"},
        "sender": {"login": "octocat"},
        "pull_request": {"number": 7, "head": {"ref": "feature-1"}},
    }
    event = GitHubClient.parse_pull_request_event(payload)
    assert event.repo == "acme/app"
    assert event.pr_number == 7
    assert event.branch == "feature-1"


def test_github_plugin_rejects_bad_signature() -> None:
    plugin = GitHubPlugin()
    plugin.configure({"token": "t", "secret": "s"})
    with pytest.raises(WebhookAuthError):
        plugin.handle_webhook(
            "push",
            {"__raw__": b"{}"},
            {"X-Hub-Signature-256": "sha256=bogus"},
        )


def test_commit_status_state_validation() -> None:
    client = GitHubClient(token="x")
    with pytest.raises(ValueError):
        client.create_commit_status("a/b", "c0ffee", "ship-it")


# --------------------------------------------------------------------------- #
# gitlab / slack
# --------------------------------------------------------------------------- #
def test_gitlab_token_check() -> None:
    assert verify_webhook_token("shared-secret", "shared-secret")
    assert not verify_webhook_token("other", "shared-secret")
    assert not verify_webhook_token("", "")


def test_slack_block_builders() -> None:
    blocks = [header("Pipeline web — FAILED"), section("*Failed stages:* test"), fields_block({"Run": "`abc`"})]
    assert blocks[0]["type"] == "header"
    assert blocks[1]["text"]["type"] == "mrkdwn"
    assert blocks[2]["fields"][0]["type"] == "mrkdwn"


def test_slack_plugin_run_blocks_lists_failures() -> None:
    run = {
        "pipeline": "web",
        "status": "FAILED",
        "trigger": "push",
        "duration": 42.0,
        "results": {"build": {"name": "build", "status": "SUCCESS"}, "test": {"name": "test", "status": "FAILED"}},
    }
    blocks = SlackPlugin.run_blocks(run)
    joined = json.dumps(blocks)
    assert "test" in joined
    assert any(block["type"] == "header" for block in blocks)


def test_slack_plugin_ack_interactive() -> None:
    ack = SlackPlugin.acknowledge_interactive({"actions": [{"action_id": "retry"}]})
    assert "retry" in ack["text"]


# --------------------------------------------------------------------------- #
# registry parsing
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("reference", "registry", "name", "tag"),
    [
        ("ubuntu:22.04", "docker.io", "library/ubuntu", "22.04"),
        ("acme/web:latest", "docker.io", "acme/web", "latest"),
        ("ghcr.io/acme/web:v2", "ghcr.io", "acme/web", "v2"),
        ("localhost:5000/ci/runner", "localhost:5000", "ci/runner", "latest"),
    ],
)
def test_parse_image_ref(reference: str, registry: str, name: str, tag: str) -> None:
    ref = parse_image_ref(reference)
    assert ref.registry == registry
    assert ref.name == name
    assert ref.tag == tag
    assert ref.digest is None


def test_parse_image_ref_with_digest() -> None:
    ref = parse_image_ref("gcr.io/proj/svc@sha256:" + "ab" * 32)
    assert ref.digest.startswith("sha256:")
    assert ref.full.endswith(ref.digest)


def test_ecr_and_gcr_ref_builders() -> None:
    assert (
        RegistryClient.build_ecr_ref("123456789012", "eu-west-1", "app", "1.0")
        == "123456789012.dkr.ecr.eu-west-1.amazonaws.com/app:1.0"
    )
    assert RegistryClient.build_gcr_ref("proj", "svc", "rc1") == "gcr.io/proj/svc:rc1"
