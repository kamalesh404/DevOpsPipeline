"""Click-based command line interface."""

from __future__ import annotations

from cli.main import cli, main
from cli.pipeline import pipeline_group
from cli.plugin import plugin_group

__all__ = ["cli", "main", "pipeline_group", "plugin_group"]
