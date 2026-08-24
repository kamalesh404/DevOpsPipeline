"""Deployment stage targeting AWS, GCP, Azure and self-hosted hosts.

Supports explicit dry-run mode (echoes commands instead of executing real
infrastructure calls) and an approval gate that skips production deployments
unless the run was explicitly approved through its variables.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from src.orchestrator.stage import BaseStage, ExecutionContext, StageResult, StageStatus

_TARGETS = frozenset({"aws", "gcp", "azure", "self_hosted"})
_ENVIRONMENTS = frozenset({"dev", "staging", "production"})
_REVISION_PATTERN = re.compile(r"revision[: ]+(\S+)", re.IGNORECASE)


@dataclass
class DeployStage(BaseStage):
    """Ship a built artifact/image to a target environment."""

    target: str = "aws"
    deploy_environment: str = "staging"
    image: Optional[str] = None
    stack: Optional[str] = None
    region: str = "us-east-1"
    bucket: Optional[str] = None
    host: Optional[str] = None
    dry_run: bool = True
    approval_required: Optional[bool] = field(default=None)

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.target not in _TARGETS:
            raise ValueError(f"unsupported target '{self.target}'; expected one of {sorted(_TARGETS)}")
        if self.deploy_environment not in _ENVIRONMENTS:
            raise ValueError(
                f"invalid environment '{self.deploy_environment}'; expected one of {sorted(_ENVIRONMENTS)}"
            )
        if self.approval_required is None:
            self.approval_required = self.deploy_environment == "production"

    def _raw_commands(self) -> list[str]:
        """Target-specific deployment commands (without dry-run prefix)."""
        if self.target == "aws":
            if self.stack:
                return [
                    f"aws cloudformation deploy --stack-name {self.stack}"
                    f" --region {self.region} --capabilities CAPABILITY_IAM"
                ]
            bucket = self.bucket or "my-deploy-bucket"
            return [f"aws s3 sync ./dist s3://{bucket} --delete"]
        if self.target == "gcp":
            image = self.image or "gcr.io/project/app:latest"
            service = self.stack or "app"
            return [
                f"gcloud run deploy {service} --image {image} --region {self.region} --quiet"
            ]
        if self.target == "azure":
            image = self.image or "registry.azurecr.io/app:latest"
            app = self.stack or "app"
            return [f"az containerapp up --name {app} --image {image} --resource-group rg-{self.deploy_environment}"]
        target_host = self.host or "app.internal"
        return [
            f"rsync -az ./dist/ {target_host}:/srv/app/",
            f"ssh {target_host} 'sudo systemctl restart app'",
        ]

    def build_commands(self, ctx: ExecutionContext) -> list[str]:
        """Return deployment commands, prefixed for dry runs when enabled."""
        commands = self._raw_commands()
        if not self.dry_run:
            return commands
        return [f'echo "[DRY-RUN] {command}"' for command in commands]

    def run(self, ctx: ExecutionContext) -> StageResult:
        """Enforce the approval gate before delegating to the base runner."""
        if self.approval_required and not ctx.variable("approved"):
            return StageResult(
                name=self.name,
                status=StageStatus.SKIPPED,
                error=f"deployment to {self.deploy_environment} requires approval",
            )
        return super().run(ctx)

    def post_process(self, ctx: ExecutionContext, result: StageResult) -> StageResult:
        """Record environment, image and detected revision in metadata."""
        result.metadata.update(
            {
                "target": self.target,
                "environment": self.deploy_environment,
                "dry_run": self.dry_run,
            }
        )
        revision_match = _REVISION_PATTERN.search(result.output)
        if revision_match:
            result.metadata["revision"] = revision_match.group(1)
        return result
