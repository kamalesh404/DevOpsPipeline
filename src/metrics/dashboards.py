"""Grafana dashboard definitions generated as JSON.

Dashboards are plain dictionaries following the Grafana JSON model so they
can be written to disk and provisioned via file-based provisioning or the
HTTP API. Queries target the metric names emitted by
:mod:`src.metrics.collector`.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

GRAFANA_SCHEMA_VERSION = 39
REFRESH_INTERVAL = "30s"


def slugify(text: str) -> str:
    """Lowercase alphanum+dashes identifier for dashboard UIDs."""
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "dashboard"


def _target(expr: str, legend: str) -> dict[str, Any]:
    """One PromQL panel target."""
    return {"expr": expr, "legendFormat": legend, "refId": "A"}


def _stat_panel(title: str, expr: str, *, x: int = 0, y: int = 0) -> dict[str, Any]:
    return {
        "type": "stat",
        "title": title,
        "gridPos": {"h": 6, "w": 8, "x": x, "y": y},
        "targets": [_target(expr, title)],
        "fieldConfig": {"defaults": {"unit": "short"}, "overrides": []},
    }


def _timeseries_panel(
    title: str,
    targets: list[tuple[str, str]],
    *,
    unit: str = "short",
    y: int = 6,
) -> dict[str, Any]:
    return {
        "type": "timeseries",
        "title": title,
        "gridPos": {"h": 8, "w": 24, "x": 0, "y": y},
        "targets": [{"expr": expr, "legendFormat": legend, "refId": chr(65 + i)} for i, (expr, legend) in enumerate(targets)],
        "fieldConfig": {"defaults": {"unit": unit}, "overrides": []},
    }


@dataclass(frozen=True)
class DashboardBuilder:
    """Assembles dashboard documents from pipeline names."""

    datasource: str = "prometheus"

    def base(self, title: str, uid: str, tags: tuple[str, ...]) -> dict[str, Any]:
        """Skeleton document with metadata fields populated."""
        return {
            "uid": uid,
            "title": title,
            "tags": list(tags),
            "schemaVersion": GRAFANA_SCHEMA_VERSION,
            "version": int(time.time()),
            "refresh": REFRESH_INTERVAL,
            "time": {"from": "now-6h", "to": "now"},
            "templating": {"list": []},
            "panels": [],
        }

    def pipeline_dashboard(self, pipeline: str) -> dict[str, Any]:
        """Per-pipeline operational view."""
        doc = self.base(f"DevOpsPipeline — {pipeline}", f"dop-{slugify(pipeline)}", ("devops", "pipeline"))
        doc["panels"] = [
            _stat_panel("Success rate (1h)", f'sum(rate(devops_pipeline_runs_total{{pipeline="{pipeline}",status="SUCCESS"}}[1h])) / sum(rate(devops_pipeline_runs_total{{pipeline="{pipeline}"}}[1h]))'),
            _stat_panel("Runs last hour", f'sum(increase(devops_pipeline_runs_total{{pipeline="{pipeline}"}}[1h]))', x=8),
            _stat_panel("Active runs", "devops_active_runs", x=16),
            _timeseries_panel(
                "Stage duration p95",
                [
                    (
                        f'histogram_quantile(0.95, sum(rate(devops_stage_duration_seconds_bucket{{pipeline="{pipeline}"}}[10m])) by (le, stage))',
                        "{{stage}}",
                    )
                ],
                unit="s",
                y=6,
            ),
            _timeseries_panel(
                "Runs by status",
                [
                    (f'sum(rate(devops_pipeline_runs_total{{pipeline="{pipeline}",status="{{status}}"}}[5m]))', "{{status}}"),
                ],
                y=14,
            ),
        ]
        return doc

    def overview_dashboard(self, pipelines: list[str]) -> dict[str, Any]:
        """Cross-pipeline fleet overview."""
        doc = self.base("DevOpsPipeline — Overview", "dop-overview", ("devops", "overview"))
        doc["panels"] = [
            _stat_panel("Total runs (1h)", 'sum(increase(devops_pipeline_runs_total[1h]))'),
            _stat_panel("Failures (1h)", 'sum(increase(devops_pipeline_runs_total{status="FAILED"}[1h]))', x=8),
            _stat_panel("Queue depth", "devops_queue_depth", x=16),
            _timeseries_panel(
                "Runs by pipeline",
                [('sum(rate(devops_pipeline_runs_total[5m])) by (pipeline)', "{{pipeline}}")],
                y=6,
            ),
        ]
        if pipelines:
            doc["templating"]["list"].append(
                {
                    "name": "pipeline",
                    "type": "query",
                    "query": "label_values(devops_pipeline_runs_total, pipeline)",
                    "includeAll": True,
                }
            )
        return doc


def validate_dashboard(document: dict[str, Any]) -> list[str]:
    """Basic structural checks; returns a list of problems found."""
    problems: list[str] = []
    if not document.get("title"):
        problems.append("missing title")
    if not document.get("uid"):
        problems.append("missing uid")
    if not isinstance(document.get("panels"), list):
        problems.append("panels must be a list")
    elif not document["panels"]:
        problems.append("no panels defined")
    for index, panel in enumerate(document.get("panels", [])):
        if "title" not in panel:
            problems.append(f"panel {index} missing title")
        if "gridPos" not in panel:
            problems.append(f"panel {index} missing gridPos")
    return problems


def write_dashboard(path: str | Path, document: dict[str, Any]) -> Path:
    """Persist a dashboard document as pretty-printed JSON."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(document, indent=2), encoding="utf-8")
    return destination
