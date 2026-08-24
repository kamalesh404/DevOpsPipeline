"""Retention policies and cleanup for artifact stores.

The manager evaluates artifacts against an age/count policy, protects keys
matching glob patterns (e.g. ``release-*``), deletes oldest-first beyond the
count cap, and reports exactly what was freed — optionally as a dry run.
"""

from __future__ import annotations

import fnmatch
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from src.artifacts.store import Artifact, ArtifactStore

LOG = logging.getLogger("devopspipeline.artifacts.retention")


@dataclass(frozen=True)
class RetentionPolicy:
    """Declarative rules deciding when artifacts may be deleted.

    An artifact survives if it is younger than ``max_age_days``, within the
    newest ``max_count`` artifacts, or its key matches any
    ``keep_key_patterns`` entry.
    """

    max_age_days: int = 30
    max_count: int = 1000
    keep_key_patterns: tuple[str, ...] = ()

    def validate(self) -> None:
        """Raise ValueError on nonsensical configuration."""
        if self.max_age_days < 1:
            raise ValueError("max_age_days must be >= 1")
        if self.max_count < 1:
            raise ValueError("max_count must be >= 1")

    def is_expired(self, artifact: Artifact, now: datetime) -> bool:
        """Whether the age rule alone would delete this artifact."""
        cutoff = now - timedelta(days=self.max_age_days)
        try:
            created = datetime.fromisoformat(artifact.created_at)
        except ValueError:
            return False
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        return created < cutoff

    def is_protected(self, key: str) -> bool:
        """Whether the key matches a keep-pattern."""
        return any(fnmatch.fnmatch(key, pattern) for pattern in self.keep_key_patterns)


@dataclass
class RetentionReport:
    """Summary of one retention evaluation/cleanup pass."""

    evaluated: int = 0
    deleted_keys: list[str] = field(default_factory=list)
    kept_count: int = 0
    freed_bytes: int = 0
    dry_run: bool = False

    @property
    def deleted_count(self) -> int:
        """Number of artifacts removed (or slated for removal in dry runs)."""
        return len(self.deleted_keys)

    def to_dict(self) -> dict[str, object]:
        """JSON-friendly representation for APIs/logs."""
        return {
            "evaluated": self.evaluated,
            "deleted": list(self.deleted_keys),
            "deleted_count": self.deleted_count,
            "kept": self.kept_count,
            "freed_bytes": self.freed_bytes,
            "dry_run": self.dry_run,
        }


class RetentionManager:
    """Applies a :class:`RetentionPolicy` to an :class:`ArtifactStore`."""

    def __init__(self, store: ArtifactStore, policy: RetentionPolicy) -> None:
        policy.validate()
        self.store = store
        self.policy = policy

    def evaluate(
        self,
        *,
        prefix: str = "",
        now: Optional[datetime] = None,
    ) -> tuple[list[str], int]:
        """Return ``(deletable_keys_sorted_oldest_first, protected_count)``."""
        now = now or datetime.now(timezone.utc)
        artifacts = sorted(self.store.list(prefix), key=lambda a: a.created_at)
        protected = [a for a in artifacts if self.policy.is_protected(a.key)]
        candidates = [a for a in artifacts if not self.policy.is_protected(a.key)]

        deletable: list[str] = []
        seen: set[str] = set()
        for artifact in candidates:
            if self.policy.is_expired(artifact, now) and artifact.key not in seen:
                deletable.append(artifact.key)
                seen.add(artifact.key)

        # Newest ``max_count`` non-protected artifacts are always retained;
        # everything beyond that window becomes deletable.
        overflow = len(candidates) - self.policy.max_count
        if overflow > 0:
            for artifact in candidates[:overflow]:
                if artifact.key not in seen:
                    deletable.append(artifact.key)
                    seen.add(artifact.key)

        return deletable, len(protected)

    def apply(
        self,
        *,
        dry_run: bool = False,
        prefix: str = "",
        now: Optional[datetime] = None,
    ) -> RetentionReport:
        """Delete expired artifacts (unless dry run) and report the outcome."""
        deletable_keys, _protected = self.evaluate(prefix=prefix, now=now)
        report = RetentionReport(evaluated=len(self.store.list(prefix)), dry_run=dry_run)

        sizes: dict[str, int] = {a.key: a.size for a in self.store.list(prefix)}
        for key in deletable_keys:
            report.deleted_keys.append(key)
            report.freed_bytes += sizes.get(key, 0)
            if not dry_run:
                if self.store.delete(key):
                    LOG.info("retention deleted artifact %s", key)
        survivors = [a.key for a in self.store.list(prefix)]
        report.kept_count = len(survivors)
        return report
