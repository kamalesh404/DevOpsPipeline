"""Integrations and the plugin hook system."""

from __future__ import annotations

from src.plugins.base import Plugin, PluginError, PluginManager, WebhookAuthError
from src.plugins.docker_registry import ImageRef, RegistryClient, parse_image_ref
from src.plugins.github import GitHubClient, GitHubPlugin, verify_webhook_signature
from src.plugins.gitlab import GitLabClient, GitLabPlugin, verify_webhook_token
from src.plugins.slack import SlackNotifier, SlackPlugin

__all__ = [
    "GitHubClient",
    "GitHubPlugin",
    "GitLabClient",
    "GitLabPlugin",
    "ImageRef",
    "Plugin",
    "PluginError",
    "PluginManager",
    "RegistryClient",
    "SlackNotifier",
    "SlackPlugin",
    "WebhookAuthError",
    "parse_image_ref",
    "verify_webhook_signature",
    "verify_webhook_token",
]
