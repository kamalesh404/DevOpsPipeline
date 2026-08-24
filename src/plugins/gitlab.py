"""GitLab integration: MR access, pipeline triggering, webhook tokens.

Mirrors the GitHub plugin surface: an ``httpx``-based REST client, shared-token
webhook authentication, and a plugin that annotates merge requests with
pipeline results.
"""

from __future__ import annotations

import hmac
import logging
from typing import Any, Mapping, Optional

from src.plugins.base import Plugin, WebhookAuthError

LOG = logging.getLogger("devopspipeline.plugins.gitlab")

GITLAB_API = "https://gitlab.com/api/v4"


class GitLabError(RuntimeError):
    """Raised when a GitLab API call fails."""


def verify_webhook_token(header_value: str, expected_token: str) -> bool:
    """Constant-time comparison of the ``X-Gitlab-Token`` header."""
    return hmac.compare_digest(header_value or "", expected_token)


class GitLabClient:
    """Minimal typed client for the GitLab REST v4 API."""

    def __init__(self, token: str, base_url: str = GITLAB_API, client: Any = None) -> None:
        self.token = token
        self.base_url = base_url.rstrip("/")
        self._client = client

    def _http(self) -> Any:
        """Lazily create the httpx client with PRIVATE-TOKEN auth."""
        if self._client is None:
            import httpx  # deferred optional dependency

            self._client = httpx.Client(
                base_url=self.base_url,
                headers={"PRIVATE-TOKEN": self.token},
                timeout=15.0,
            )
        return self._client

    def _request(self, method: str, path: str, json: dict[str, Any] | None = None) -> dict[str, Any]:
        response = self._http().request(method, path, json=json)
        if response.status_code >= 400:
            raise GitLabError(f"{method} {path} -> {response.status_code}: {response.text[:200]}")
        return response.json() if response.content else {}

    @staticmethod
    def _project_path(project_id: int | str) -> str:
        """URL-encode a numeric id or 'group/project' path."""
        from urllib.parse import quote

        return str(quote(str(project_id), safe=""))

    def get_merge_request(self, project_id: int | str, mr_iid: int) -> dict[str, Any]:
        """Fetch one merge request."""
        return self._request("GET", f"/projects/{self._project_path(project_id)}/merge_requests/{mr_iid}")

    def accept_merge_request(
        self,
        project_id: int | str,
        mr_iid: int,
        *,
        remove_source_branch: bool = True,
    ) -> dict[str, Any]:
        """Merge an MR once pipelines pass."""
        return self._request(
            "PUT",
            f"/projects/{self._project_path(project_id)}/merge_requests/{mr_iid}/merge",
            {"merge_when_pipeline_succeeds": True, "should_remove_source_branch": remove_source_branch},
        )

    def add_merge_request_note(self, project_id: int | str, mr_iid: int, body: str) -> dict[str, Any]:
        """Comment on a merge request."""
        return self._request(
            "POST",
            f"/projects/{self._project_path(project_id)}/merge_requests/{mr_iid}/notes",
            {"body": body},
        )

    def create_pipeline(
        self,
        project_id: int | str,
        ref: str,
        variables: Optional[Mapping[str, str]] = None,
    ) -> dict[str, Any]:
        """Trigger a new pipeline for ``ref`` with optional variables."""
        payload: dict[str, Any] = {
            "ref": ref,
            "variables": [{"key": k, "value": v} for k, v in (variables or {}).items()],
        }
        return self._request("POST", f"/projects/{self._project_path(project_id)}/pipeline", payload)

    def get_pipeline(self, project_id: int | str, pipeline_id: int) -> dict[str, Any]:
        """Fetch pipeline status details."""
        return self._request("GET", f"/projects/{self._project_path(project_id)}/pipelines/{pipeline_id}")


class GitLabPlugin(Plugin):
    """Bridges GitLab webhooks/MRs with DevOpsPipeline runs."""

    def __init__(self, name: str = "gitlab") -> None:
        super().__init__(name=name, version="1.0.0")
        self.client: GitLabClient | None = None
        self.webhook_token: str = ""
        self.project_id: int | str | None = None

    def configure(self, config: Mapping[str, Any]) -> None:
        super().configure(config)
        token = str(config.get("token", ""))
        self.webhook_token = str(config.get("webhook_token", ""))
        self.project_id = config.get("project_id")
        base_url = str(config.get("base_url", GITLAB_API))
        self.client = GitLabClient(token, base_url=base_url) if token else None

    def handle_webhook(
        self,
        event_type: str,
        payload: Mapping[str, Any],
        headers: Mapping[str, str],
    ) -> dict[str, Any]:
        """Validate the shared token and summarize the event."""
        if self.webhook_token and not verify_webhook_token(
            headers.get("X-Gitlab-Token", ""), self.webhook_token
        ):
            raise WebhookAuthError("invalid GitLab webhook token")
        project = (payload.get("project") or {}).get("path_with_namespace", "")
        user = (payload.get("user") or {}).get("username", "")
        summary = {
            "plugin": self.name,
            "object_kind": event_type,
            "project": project,
            "user": user,
        }
        if event_type == "merge_request":
            attributes = payload.get("object_attributes") or {}
            summary["mr_iid"] = attributes.get("iid")
            summary["action"] = attributes.get("action")
        elif event_type == "push":
            summary["ref"] = str(payload.get("ref", "")).removeprefix("refs/heads/")
        return summary

    def on_pipeline_complete(self, run: Mapping[str, Any]) -> None:
        """Leave a success/failure note on the configured MR, if any."""
        if self.client is None or self.project_id is None:
            return
        mr_iid = run.get("event", {}).get("mr_iid")
        if not mr_iid:
            return
        icon = "✅" if run.get("status") == "SUCCESS" else "❌"
        try:
            self.client.add_merge_request_note(
                self.project_id,
                int(mr_iid),
                f"{icon} DevOpsPipeline `{run.get('pipeline')}` finished: {run.get('status')}",
            )
        except Exception:
            LOG.exception("failed to annotate MR %s", mr_iid)
