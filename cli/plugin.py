"""Plugin management commands: list, install, enable, disable, info."""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from pathlib import Path
from typing import Any

import click

from src.plugins.base import Plugin, PluginManager

LOG = logging.getLogger("devopspipeline.cli.plugin")

BUILTIN_PLUGINS: tuple[str, ...] = ("github", "gitlab", "slack", "docker_registry")
CONFIG_DIR = Path.home() / ".devopspipeline"
PLUGIN_CONFIG = CONFIG_DIR / "plugins.json"


class PluginConfig:
    """JSON file persisting which plugins are enabled and their settings."""

    def __init__(self, path: Path = PLUGIN_CONFIG) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> dict[str, Any]:
        """Read plugin state (empty when missing/corrupt)."""
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            LOG.warning("plugin config corrupt; starting fresh")
            return {}

    def save(self, data: dict[str, Any]) -> None:
        """Persist plugin state atomically enough for CLI purposes."""
        self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def is_enabled(self, name: str) -> bool:
        """Default-enabled unless explicitly disabled."""
        entry = self.load().get(name)
        if isinstance(entry, dict):
            return bool(entry.get("enabled", True))
        return True

    def set_enabled(self, name: str, enabled: bool) -> None:
        """Record enabled/disabled state for one plugin."""
        data = self.load()
        entry = data.get(name)
        if not isinstance(entry, dict):
            entry = {"enabled": enabled}
        else:
            entry["enabled"] = enabled
        data[name] = entry
        self.save(data)


@click.group()
def plugin_group() -> None:
    """Manage DevOpsPipeline plugins."""


@plugin_group.command("list")
@click.option("--all", "show_all", is_flag=True, help="Include discovered third-party plugins.")
def list_plugins(show_all: bool) -> None:
    """List built-in (and optionally discoverable) plugins."""
    config = PluginConfig()
    rows: list[tuple[str, str]] = [(name, config.is_enabled(name)) for name in BUILTIN_PLUGINS]
    if show_all:
        manager = PluginManager()
        for plugin_name, plugin_class in sorted(PluginManager.discover().items()):
            instance = plugin_class(name=plugin_name)
            rows.append((plugin_name, config.is_enabled(plugin_name)))
            click.secho(f"  discovered: {instance.describe()}", dim=True)
    for plugin_name, enabled in rows:
        marker = click.style("enabled ", fg="green") if enabled else click.style("disabled", fg="red")
        origin = "builtin" if plugin_name in BUILTIN_PLUGINS else "external"
        click.echo(f"{marker}  {plugin_name:<20} [{origin}]")


@plugin_group.command("enable")
@click.argument("name")
def enable_plugin(name: str) -> None:
    """Enable event delivery to a plugin."""
    PluginConfig().set_enabled(name, True)
    click.echo(f"plugin '{name}' enabled")


@plugin_group.command("disable")
@click.argument("name")
def disable_plugin(name: str) -> None:
    """Disable a plugin without uninstalling it."""
    PluginConfig().set_enabled(name, False)
    click.echo(f"plugin '{name}' disabled")


@plugin_group.command("install")
@click.argument("package")
def install_plugin(package: str) -> None:
    """Install a plugin distribution via pip into the current environment."""
    if not package.startswith("devopspipeline-"):
        click.confirm(
            f"'{package}' does not look like a devopspipeline plugin; install anyway?",
            abort=True,
        )
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", package],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise click.ClickException(result.stderr.strip() or f"pip failed with code {result.returncode}")
    discovered = PluginManager.discover()
    click.secho(f"installed '{package}'", fg="green")
    if discovered:
        click.echo(f"entry points now advertised: {', '.join(sorted(discovered))}")
    else:
        click.echo("(no new entry points detected — check the package metadata)")


@plugin_group.command("info")
@click.argument("name")
def info_plugin(name: str) -> None:
    """Show details about one plugin from discovery or builtins."""
    discovered = PluginManager.discover()
    plugin_class = discovered.get(name)
    if plugin_class is None and name in BUILTIN_PLUGINS:
        mapping: dict[str, type[Plugin]] = {}
        try:
            from src.plugins.github import GitHubPlugin
            from src.plugins.gitlab import GitLabPlugin
            from src.plugins.slack import SlackPlugin

            mapping.update({"github": GitHubPlugin, "gitlab": GitLabPlugin, "slack": SlackPlugin})
        except ImportError:  # pragma: no cover
            pass
        plugin_class = mapping.get(name)
    if plugin_class is None:
        raise click.ClickException(f"plugin '{name}' not found")
    instance = plugin_class(name=name)
    for key, value in instance.describe().items():
        click.echo(f"{key:<10}: {value}")
