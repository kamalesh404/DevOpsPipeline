"""Plugin framework: lifecycle hooks, registry and discovery.

Plugins observe pipeline events (start/stage/complete) and can handle
inbound webhooks. The :class:`PluginManager` registers plugins by name,
isolates hook failures, and supports entry-point based discovery for
third-party distributions.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

LOG = logging.getLogger("devopspipeline.plugins")

ENTRY_POINT_GROUP = "devopspipeline.plugins"


@dataclass
class Plugin:
    """Base class every DevOpsPipeline plugin must extend.

    Subclasses override whichever hooks they care about; all hooks receive
    JSON-serializable dictionaries so plugins stay decoupled from engine
    internals.
    """

    name: str = "plugin"
    version: str = "0.1.0"
    enabled: bool = True
    config: dict[str, Any] = field(default_factory=dict)

    def configure(self, config: Mapping[str, Any]) -> None:
        """Apply configuration; called before the plugin receives events."""
        self.config = dict(config)

    def on_pipeline_start(self, run: Mapping[str, Any]) -> None:
        """Invoked when a pipeline run begins."""

    def on_stage_complete(self, result: Mapping[str, Any]) -> None:
        """Invoked after each stage finishes."""

    def on_pipeline_complete(self, run: Mapping[str, Any]) -> None:
        """Invoked once when a pipeline run reaches a terminal state."""

    def handle_webhook(
        self,
        event_type: str,
        payload: Mapping[str, Any],
        headers: Mapping[str, str],
    ) -> Optional[dict[str, Any]]:
        """Handle an inbound webhook; return an optional ack response."""
        return None

    def describe(self) -> dict[str, Any]:
        """Metadata used by listings and dashboards."""
        return {
            "name": self.name,
            "version": self.version,
            "enabled": self.enabled,
            "module": type(self).__module__,
            "class": type(self).__name__,
        }


class PluginError(RuntimeError):
    """Raised for registration conflicts or misconfigured plugins."""


class PluginManager:
    """Registry that fans lifecycle events out to enabled plugins."""

    def __init__(self) -> None:
        self._plugins: dict[str, Plugin] = {}

    # -- registry ---------------------------------------------------------
    def register(self, plugin: Plugin, *, replace: bool = False) -> Plugin:
        """Add ``plugin`` under its own name (optionally replacing)."""
        if not isinstance(plugin, Plugin):
            raise PluginError(f"{plugin!r} is not a Plugin instance")
        if plugin.name in self._plugins and not replace:
            raise PluginError(f"plugin '{plugin.name}' already registered")
        self._plugins[plugin.name] = plugin
        LOG.debug("registered plugin '%s'", plugin.name)
        return plugin

    def unregister(self, name: str) -> bool:
        """Remove a plugin by name; False when it was absent."""
        return self._plugins.pop(name, None) is not None

    def get(self, name: str) -> Optional[Plugin]:
        """Look up a plugin by name."""
        return self._plugins.get(name)

    @property
    def names(self) -> list[str]:
        """Registered plugin names in insertion order."""
        return list(self._plugins)

    def enable(self, name: str) -> None:
        """Enable event delivery to ``name``."""
        self._require(name).enabled = True

    def disable(self, name: str) -> None:
        """Disable event delivery to ``name`` without unregistering."""
        self._require(name).enabled = False

    def _require(self, name: str) -> Plugin:
        plugin = self._plugins.get(name)
        if plugin is None:
            raise PluginError(f"unknown plugin '{name}'")
        return plugin

    def list_plugins(self) -> list[dict[str, Any]]:
        """Describe every registered plugin."""
        return [plugin.describe() for plugin in self._plugins.values()]

    # -- event fan-out ----------------------------------------------------
    def _for_each_enabled(self, hook_name: str, payload: Mapping[str, Any]) -> int:
        delivered = 0
        for plugin in self._plugins.values():
            if not plugin.enabled:
                continue
            try:
                getattr(plugin, hook_name)(payload)
                delivered += 1
            except Exception:
                LOG.exception("plugin '%s' failed in %s", plugin.name, hook_name)
        return delivered

    def fire_pipeline_start(self, run: Mapping[str, Any]) -> int:
        """Notify plugins that a run started; returns delivery count."""
        return self._for_each_enabled("on_pipeline_start", run)

    def fire_stage_complete(self, result: Mapping[str, Any]) -> int:
        """Notify plugins about a finished stage; returns delivery count."""
        return self._for_each_enabled("on_stage_complete", result)

    def fire_pipeline_complete(self, run: Mapping[str, Any]) -> int:
        """Notify plugins that a run finished; returns delivery count."""
        return self._for_each_enabled("on_pipeline_complete", run)

    def dispatch_webhook(
        self,
        source: str,
        event_type: str,
        payload: Mapping[str, Any],
        headers: Mapping[str, str],
    ) -> list[dict[str, Any]]:
        """Forward a webhook to matching enabled plugins; collect responses."""
        responses: list[dict[str, Any]] = []
        for plugin in self._plugins.values():
            if not plugin.enabled or plugin.name != source:
                continue
            try:
                response = plugin.handle_webhook(event_type, payload, headers)
                if response is not None:
                    responses.append(response)
            except Exception as exc:
                LOG.exception("plugin '%s' webhook handling failed", plugin.name)
                responses.append({"plugin": plugin.name, "error": str(exc)})
        return responses

    # -- discovery --------------------------------------------------------
    @staticmethod
    def discover(group: str = ENTRY_POINT_GROUP) -> dict[str, type[Plugin]]:
        """Load plugin classes advertised via package entry points.

        Returns a mapping of advertised plugin name to class. Failures are
        logged and skipped so one broken distribution cannot take down the
        manager.
        """
        found: dict[str, type[Plugin]] = {}
        try:
            from importlib.metadata import entry_points

            eps = entry_points(group=group)
        except Exception:  # pragma: no cover - very old interpreters
            return found
        for entry_point in eps:
            try:
                candidate = entry_point.load()
                if isinstance(candidate, type) and issubclass(candidate, Plugin):
                    found[entry_point.name] = candidate
            except Exception:
                LOG.warning("failed to load entry point '%s'", entry_point.name)
        return found


class WebhookAuthError(PermissionError):
    """Raised when a webhook fails signature/token verification."""
