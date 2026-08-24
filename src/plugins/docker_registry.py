"""Container registry integration: Docker Hub, ECR and GCR.

Parses fully-qualified image references (tags and digests), wraps the
``docker`` CLI for login/push/pull using ``--password-stdin``, and offers
helpers to build ECR/GCR image names. A plugin subclass can push images
automatically after successful build stages.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass

LOG = logging.getLogger("devopspipeline.plugins.registry")


class RegistryError(RuntimeError):
    """Raised when a registry CLI operation fails."""


@dataclass(frozen=True)
class ImageRef:
    """A parsed container image reference."""

    registry: str
    namespace: str
    repository: str
    tag: str
    digest: str | None = None

    @property
    def name(self) -> str:
        """``namespace/repository`` portion."""
        return f"{self.namespace}/{self.repository}" if self.namespace else self.repository

    @property
    def full(self) -> str:
        """Complete reference including tag or digest."""
        base = f"{self.registry}/{self.name}:{self.tag}"
        if self.digest:
            base += f"@{self.digest}"
        return base


def parse_image_ref(
    reference: str,
    *,
    default_registry: str = "docker.io",
    default_tag: str = "latest",
) -> ImageRef:
    """Parse an image reference into its components.

    Handles optional scheme prefixes, explicit registries (host[:port]),
    namespaces, tags and ``@sha256:...`` digests.
    """
    ref = reference.strip().removeprefix("https://").removeprefix("http://")
    digest: str | None = None
    if "@" in ref:
        ref, digest = ref.rsplit("@", 1)
    registry = default_registry
    path = ref
    if "/" in ref:
        first, rest = ref.split("/", 1)
        if "." in first or ":" in first or first == "localhost":
            registry, path = first, rest
    namespace, _, repository = path.rpartition("/")
    tag = default_tag
    if ":" in repository:
        repository, _, tag = repository.rpartition(":")
    return ImageRef(registry=registry, namespace=namespace, repository=repository, tag=tag, digest=digest)


@dataclass
class CmdResult:
    """Outcome of one CLI invocation."""

    code: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        """Whether the command exited zero."""
        return self.code == 0


class RegistryClient:
    """Thin wrapper over the docker CLI for registry operations."""

    def __init__(self, docker_binary: str = "docker") -> None:
        self.docker_binary = docker_binary

    def _run(self, args: list[str], *, input_text: str | None = None) -> CmdResult:
        """Execute a docker subcommand capturing output."""
        completed = subprocess.run(  # noqa: S603 - fixed binary + args
            [self.docker_binary, *args],
            capture_output=True,
            text=True,
            input=input_text,
            timeout=900,
            check=False,
        )
        result = CmdResult(completed.returncode, completed.stdout, completed.stderr)
        if not result.ok and input_text is None:
            LOG.debug("docker %s failed: %s", args[0], result.stderr.strip())
        return result

    def login(self, registry: str, username: str, password: str) -> CmdResult:
        """Authenticate against a registry using stdin for the password."""
        outcome = self._run(
            ["login", registry, "--username", username, "--password-stdin"],
            input_text=password,
        )
        if not outcome.ok:
            raise RegistryError(f"login to {registry} failed: {outcome.stderr.strip()}")
        return outcome

    def push(self, reference: str) -> CmdResult:
        """Push an image reference."""
        outcome = self._run(["push", reference])
        if not outcome.ok:
            raise RegistryError(f"push failed for {reference}: {outcome.stderr.strip()}")
        return outcome

    def pull(self, reference: str) -> CmdResult:
        """Pull an image reference."""
        outcome = self._run(["pull", reference])
        if not outcome.ok:
            raise RegistryError(f"pull failed for {reference}: {outcome.stderr.strip()}")
        return outcome

    def tag(self, source: str, target: str) -> CmdResult:
        """Create a new tag pointing at ``source``."""
        outcome = self._run(["tag", source, target])
        if not outcome.ok:
            raise RegistryError(f"tag {source} -> {target} failed: {outcome.stderr.strip()}")
        return outcome

    @staticmethod
    def build_ecr_ref(account_id: str, region: str, repository: str, tag: str) -> str:
        """Assemble an ECR image reference."""
        return f"{account_id}.dkr.ecr.{region}.amazonaws.com/{repository}:{tag}"

    @staticmethod
    def build_gcr_ref(project: str, name: str, tag: str, host: str = "gcr.io") -> str:
        """Assemble a GCR/AR image reference."""
        return f"{host}/{project}/{name}:{tag}"
