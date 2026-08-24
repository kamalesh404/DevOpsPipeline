"""Slack integration: Block Kit messages, incoming webhooks, interactivity.

Builds rich notification payloads, posts them via incoming-webhook URLs, and
acknowledges interactive message callbacks. HTTP posting goes through a small
helper that tests can monkeypatch, keeping the module dependency-light.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from typing import Any, Deque, Mapping

from src.plugins.base import Plugin

LOG = logging.getLogger("devopspipeline.plugins.slack")

STATUS_COLORS = {"SUCCESS": "#36a64f", "FAILED": "#dc3545", "CANCELLED": "#ecc94b", "SKIPPED": "#9ca3af"}


def section(text: str) -> dict[str, Any]:
    """A mrkdwn text section block."""
    return {"type": "section", "text": {"type": "mrkdwn", "text": text[:2900]}}


def header(text: str) -> dict[str, Any]:
    """A plain-text header block (Slack caps these at 150 chars)."""
    return {"type": "header", "text": {"type": "plain_text", "text": text[:150], "emoji": False}}


def divider() -> dict[str, Any]:
    """A visual divider block."""
    return {"type": "divider"}


def fields_block(fields: Mapping[str, str]) -> dict[str, Any]:
    """A two-column field section block."""
    return {
        "type": "section",
        "fields": [
            {"type": "mrkdwn", "text": f"*{key}:*\n{value}"}
            for key, value in list(fields.items())[:10]
        ],
    }


def button(text: str, url: str, action_id: str = "open") -> dict[str, Any]:
    """An action block containing a single link-style button."""
    return {
        "type": "actions",
        "elements": [{"type": "button", "text": {"type": "plain_text", "text": text[:75]}, "url": url, "action_id": action_id}],
    }


@dataclass
class SlackNotifier:
    """Posts messages to Slack through an incoming webhook."""

    webhook_url: str
    channel: str | None = None
    username: str = "DevOpsPipeline"

    def build_payload(self, blocks: list[dict[str, Any]], fallback: str, color: str = "#36a64f") -> dict[str, Any]:
        """Assemble the webhook JSON body with an attachment color."""
        payload: dict[str, Any] = {
            "username": self.username,
            "attachments": [{"color": color, "blocks": blocks}],
            "text": fallback,
        }
        if self.channel:
            payload["channel"] = self.channel
        return payload

    def send_blocks(self, blocks: list[dict[str, Any]], fallback: str = "", color: str = "#36a64f") -> bool:
        """Post blocks to the webhook; returns success as a boolean."""
        try:
            self._post(self.build_payload(blocks, fallback or fallback_text(blocks), color))
            return True
        except Exception:
            LOG.exception("slack delivery failed")
            return False

    @staticmethod
    def _post(payload: dict[str, Any]) -> bytes:
        """HTTP POST via httpx; isolated for test monkeypatching."""
        import httpx  # deferred optional dependency

        response = httpx.post("", json=payload, timeout=10.0)
        response.raise_for_status()
        return response.content


def fallback_text(blocks: list[dict[str, Any]]) -> str:
    """Derive a plain-text fallback from the first section block."""
    for block in blocks:
        if block.get("type") == "section":
            text = block.get("text", {}).get("text", "")
            if text:
                return text
    return "DevOpsPipeline notification"


class SlackPlugin(Plugin):
    """Notifies configured channels when runs finish."""

    def __init__(self, name: str = "slack") -> None:
        super().__init__(name=name, version="1.0.0")
        self.notifier: SlackNotifier | None = None
        self.notify_on: set[str] = {"SUCCESS", "FAILED"}
        self.history: Deque[dict[str, Any]] = deque(maxlen=50)

    def configure(self, config: Mapping[str, Any]) -> None:
        super().configure(config)
        webhook_url = str(config.get("webhook_url", ""))
        self.notifier = (
            SlackNotifier(webhook_url, channel=config.get("channel")) if webhook_url else None
        )
        self.notify_on = set(config.get("notify_on", self.notify_on))

    @staticmethod
    def run_blocks(run: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Render pipeline-run result blocks."""
        status = str(run.get("status", "RUNNING"))
        color = STATUS_COLORS.get(status, "#808080")
        failed = [r["name"] for r in (run.get("results") or {}).values() if r.get("status") != "SUCCESS"]
        blocks = [header(f"Pipeline {run.get('pipeline', '?')} — {status}")]
        blocks.append(
            fields_block(
                {
                    "Run": f"`{run.get('id', 'n/a')}`",
                    "Trigger": str(run.get("trigger", "manual")),
                    "Duration": f"{float(run.get('duration', 0.0)):.1f}s",
                    "Status": status,
                }
            )
        )
        if failed:
            blocks.append(section(f"*Failed stages:* {', '.join(failed)}"))
        blocks.append(divider())
        return blocks

    def on_pipeline_complete(self, run: Mapping[str, Any]) -> None:
        """Send the summary when the terminal status matches notify_on."""
        status = str(run.get("status", ""))
        record = {"pipeline": run.get("pipeline"), "status": status}
        self.history.append(record)
        if self.notifier is None or status not in self.notify_on:
            return
        color = STATUS_COLORS.get(status, "#808080")
        self.notifier.send_blocks(
            self.run_blocks(run), fallback=f"{record['pipeline']}: {status}", color=color
        )

    @staticmethod
    def acknowledge_interactive(payload: Mapping[str, Any]) -> dict[str, Any]:
        """Respond to Slack interactive callbacks within the 3s window."""
        actions = payload.get("actions") or []
        action_name = actions[0].get("action_id", "") if actions else ""
        return {"text": f"Acknowledged action `{action_name}`"}
