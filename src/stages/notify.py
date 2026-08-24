"""Notification stage delivering run summaries to Slack, email and webhooks.

Slack/webhook delivery uses plain ``urllib`` (no extra dependency); the HTTP
helper is a small static method that tests can monkeypatch. Message rendering
supports ``{pipeline}``, ``{run_id}``, ``{status}`` and ``{message}`` fields
drawn from the execution context.
"""

from __future__ import annotations

import json
import smtplib
import urllib.request
from dataclasses import dataclass, field
from email.mime.text import MIMEText
from typing import Any

from src.orchestrator.stage import BaseStage, ExecutionContext, StageResult, StageStatus

_CHANNELS = frozenset({"slack", "email", "webhook"})
DEFAULT_TEMPLATE = "[{pipeline}] run {run_id} finished with status {status}"


class _SafeFormatDict(dict[str, Any]):
    """Dictionary whose missing keys format as empty instead of raising."""

    def __missing__(self, key: str) -> str:
        return f"{{{key}}}"


@dataclass
class NotifyStage(BaseStage):
    """Send pipeline notifications to one or more channels."""

    channels: tuple[str, ...] = ("slack",)
    webhook_url: str | None = None
    slack_channel: str | None = None
    email_recipients: tuple[str, ...] = ()
    subject_template: str = DEFAULT_TEMPLATE
    message_template: str = DEFAULT_TEMPLATE
    endpoint_timeout: float = 5.0
    extra_payload: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        super().__post_init__()
        unknown = set(self.channels) - _CHANNELS
        if unknown:
            raise ValueError(f"unknown notification channels {sorted(unknown)}; expected {sorted(_CHANNELS)}")

    @staticmethod
    def slack_blocks(title: str, text: str, color: str = "#36a64f") -> list[dict[str, Any]]:
        """Build a minimal Block Kit attachment payload."""
        return [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": title[:150], "emoji": False},
            },
            {"type": "section", "text": {"type": "mrkdwn", "text": text[:2900]}},
            {"type": "context", "elements": [{"type": "mrkdwn", "text": f"color: {color}"}]},
        ]

    @staticmethod
    def _http_post_json(url: str, payload: dict[str, Any], timeout: float) -> bytes:
        """POST JSON to an endpoint; overridable in tests."""
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return response.read()

    def _render(self, ctx: ExecutionContext) -> str:
        """Render the message template against context variables."""
        fields = _SafeFormatDict(
            pipeline=ctx.pipeline_name,
            run_id=ctx.run_id,
            status=ctx.variable("status", "RUNNING"),
            message=ctx.variable("message", ""),
        )
        fields.update(ctx.variables)
        return self.message_template.format_map(fields)

    def _deliver(self, ctx: ExecutionContext, message: str) -> list[str]:
        """Attempt delivery on every configured channel; return error notes."""
        notes: list[str] = []
        if "slack" in self.channels or "webhook" in self.channels:
            url = self.webhook_url or ""
            if not url:
                notes.append("slack/webhook skipped: no webhook_url configured")
            else:
                try:
                    self._http_post_json(
                        url,
                        {
                            "text": message,
                            "channel": self.slack_channel,
                            "blocks": self.slack_blocks(
                                f"Pipeline {ctx.pipeline_name}",
                                message,
                            ),
                            **self.extra_payload,
                        },
                        self.endpoint_timeout,
                    )
                except Exception as exc:
                    notes.append(f"http delivery failed: {exc}")
        if "email" in self.channels:
            smtp_host = ctx.environment.get("DEVOPS_SMTP_HOST")
            if not smtp_host or not self.email_recipients:
                notes.append("email skipped: SMTP host or recipients not configured")
            else:
                try:
                    client = smtplib.SMTP(smtp_host, int(ctx.environment.get("DEVOPS_SMTP_PORT", "25")))
                    try:
                        mail = MIMEText(message)
                        mail["Subject"] = self.subject_template.format_map(
                            _SafeFormatDict(pipeline=ctx.pipeline_name, run_id=ctx.run_id)
                        )
                        mail["To"] = ", ".join(self.email_recipients)
                        client.sendmail("devopspipeline@localhost", list(self.email_recipients), mail.as_string())
                    finally:
                        client.quit()
                except Exception as exc:
                    notes.append(f"email delivery failed: {exc}")
        return notes

    def build_commands(self, ctx: ExecutionContext) -> list[str]:  # pragma: no cover
        """Notifications are delivered in-process, not via shell."""
        return []

    def run(self, ctx: ExecutionContext) -> StageResult:
        """Render and deliver notifications; never fails the pipeline hard."""
        started = StageResult(name=self.name, status=StageStatus.RUNNING)
        message = self._render(ctx)
        if not any(channel in self.channels for channel in ("slack", "webhook", "email")):
            return StageResult(name=self.name, status=StageStatus.SKIPPED, error="no channels enabled")
        notes = self._deliver(ctx, message)
        started.output = message
        started.metadata["delivery_notes"] = notes
        started.status = StageStatus.SUCCESS if not notes else StageStatus.FAILED
        started.error = "; ".join(notes)
        return started
