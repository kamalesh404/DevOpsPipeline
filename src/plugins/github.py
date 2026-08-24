"""GitHub integration: webhook verification, commit statuses, PR access.

Provides an API client on top of ``httpx`` plus a plugin that authenticates
inbound webhooks with HMAC-SHA256 signatures and reports pipeline results
back as commit statuses.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from src.plugins.base import Plugin, WebhookAuthError

LOG = logging.getLogger("devopspipeline.plugins.github")

GITHUB_API = "https://api.github.com"
VALID_STATES = frozenset({"error", "failure", "pending", "success"})


class GitHubError(RuntimeError):
    """Raised when a GitHub REST call fails."""


@dataclass
class WebhookEvent:
    """Normalized view of an inbound GitHub webhook."""

    kind: str
    action: str
    repo: str
    branch: str
    pr_number: Optional[int]
    sender: str
    raw: dict[str, Any] = field(default_factory=dict)


def verify_webhook_signature(payload_body: bytes, signature_header: str, secret: str) -> bool:
    """Constant-time check of an ``X-Hub-Signature-256`` header."""
    if not signature_header.startswith("sha256="):
        return False
    digest = hmac.new(secret.encode("utf-8"), payload_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"sha256={digest}", signature_header)


class GitHubClient:
    """Thin typed wrapper over the GitHub REST endpoints we need."""

    def __init__(self, token: str, api_url: str = GITHUB_API, client: Any = None) -> None:
        self.token = token
        self.api_url = api_url.rstrip("/")
        self._client = client

    def _http(self) -> Any:
        """Lazily create the httpx client with auth headers."""
        if self._client is None:
            import httpx  # deferred optional dependency

            self._client = httpx.Client(
                base_url=self.api_url,
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Accept": "application/vnd.github+json",
                },
                timeout=15.0,
            )
        return self._client

    def _request(self, method: str, path: str, json: dict[str, Any] | None = None) -> dict[str, Any]:
        response = self._http().request(method, path, json=json)
        if response.status_code >= 400:
            raise GitHubError(f"{method} {path} -> {response.status_code}: {response.text[:200]}")
        return response.json() if response.content else {}

    def create_commit_status(
        self,
        repo: str,
        sha: str,
        state: str,
        *,
        target_url: str | None = None,
        context: str = "ci/devopspipeline",
        description: str = "",
    ) -> dict[str, Any]:
        """Publish a status on a commit (success/failure/pending/error)."""
        if state not in VALID_STATES:
            raise ValueError(f"invalid status state '{state}'")
        body: dict[str, Any] = {"state": state, "context": context}
        if target_url:
            body["target_url"] = target_url
        if description:
            body["description"] = description[:140]
        return self._request("POST", f"/repos/{repo}/statuses/{sha}", body)

    def add_issue_comment(self, repo: str, number: int, body: str) -> dict[str, Any]:
        """Comment on a PR/issue thread."""
        return self._request("POST", f"/repos/{repo}/issues/{number}/comments", {"body": body})

    def get_pull_request(self, repo: str, number: int) -> dict[str, Any]:
        """Fetch pull request metadata."""
        return self._request("GET", f"/repos/{repo}/pulls/{number}")

    @staticmethod
    def parse_pull_request_event(payload: Mapping[str, Any]) -> WebhookEvent:
        """Normalize a ``pull_request`` webhook payload."""
        pr = payload.get("pull_request") or {}
        return WebhookEvent(
            kind="pull_request",
            action=str(payload.get("action", "")),
            repo=str((payload.get("repository") or {}).get("full_name", "")),
            branch=str((pr.get("head") or {}).get("ref", "")),
            pr_number=int(pr.get("number")) if pr.get("number") else None,
            sender=str((payload.get("sender") or {}).get("login", "")),
            raw=dict(payload),
        )


class GitHubPlugin(Plugin):
    """Reports run outcomes to GitHub and verifies inbound webhooks."""

    def __init__(self, name: str = "github") -> None:
        super().__init__(name=name, version="1.0.0")
        self.client: GitHubClient | None = None
        self.secret: str = ""
        self.context: str = "ci/devopspipeline"
        self.last_events: list[WebhookEvent] = []

    def configure(self, config: Mapping[str, Any]) -> None:
        super().configure(config)
        token = str(config.get("token", ""))
        self.secret = str(config.get("secret", ""))
        self.context = str(config.get("context", self.context))
        self.client = GitHubClient(token) if token else None

    def handle_webhook(
        self,
        event_type: str,
        payload: Mapping[str, Any],
        headers: Mapping[str, str],
    ) -> dict[str, Any]:
        """Verify the signature then normalize the event."""
        signature = headers.get("X-Hub-Signature-256", "")
        raw_body = bytes(payload.get("__raw__", b"")) or b"{}"
        if self.secret and not verify_webhook_signature(raw_body, signature, self.secret):
            raise WebhookAuthError("invalid GitHub webhook signature")
        event = (
            GitHubPlugin.parse_pull_request_event(payload)
            if event_type == "pull_request"
            else WebhookEvent(
                kind=event_type,
                action=str(payload.get("action", "")),
                repo=str((payload.get("repository") or {}).get("full_name", "")),
                branch=str(((payload.get("push") or {}).get("ref", "")).removeprefix("refs/heads/")),
                pr_number=None,
                sender=str((payload.get("sender") or {}).get("login", "")),
            )
        )
        self.last_events.append(event)
        return {"plugin": self.name, "event": event.kind, "action": event.action}

    def on_pipeline_complete(self, run: Mapping[str, Any]) -> None:
        """Post a commit status when the triggering SHA is known."""
        if self.client is None:
            LOG.debug("github plugin has no token; skipping status update")
            return
        sha = str(run.get("event", {}).get("commit_sha", ""))
        repo = str(run.get("event", {}).get("repo", ""))
        if not sha or not repo:
            return
        state = "success" if run.get("status") == "SUCCESS" else "failure"
        try:
            self.client.create_commit_status(
                repo,
                sha,
                state,
                context=self.context,
                description=f"pipeline {run.get('pipeline')} {state}",
            )
        except Exception:
            LOG.exception("failed to publish status for %s@%s", repo, sha[:7])
