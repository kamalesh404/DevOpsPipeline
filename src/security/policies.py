"""RBAC policies, pipeline permissions and the audit log.

Roles map to permission sets; :class:`RBACPolicy` evaluates access requests
against those sets plus context rules (protected branches, production
deployments). Every decision is appended to an append-only
:class:`AuditLog` that can persist as JSON Lines.
"""

from __future__ import annotations

import fnmatch
import json
import threading
import time
from dataclasses import dataclass, field
from enum import Enum, Flag, auto
from pathlib import Path
from typing import Any


class Permission(Flag):
    """Atomic capabilities checked by the platform."""

    PIPELINE_VIEW = auto()
    PIPELINE_RUN = auto()
    PIPELINE_EDIT = auto()
    PIPELINE_DELETE = auto()
    SECRET_READ = auto()
    SECRET_WRITE = auto()
    DEPLOY_STAGING = auto()
    DEPLOY_PROD = auto()
    PLUGIN_MANAGE = auto()
    ADMIN = auto()


class Role(Enum):
    """Coarse-grained user roles."""

    VIEWER = "viewer"
    DEVELOPER = "developer"
    MAINTAINER = "maintainer"
    ADMIN = "admin"


_ROLE_LADDER: list[tuple[Role, Permission]] = [
    (Role.VIEWER, Permission.PIPELINE_VIEW),
    (Role.DEVELOPER, Permission.PIPELINE_RUN | Permission.PIPELINE_EDIT | Permission.DEPLOY_STAGING),
    (Role.MAINTAINER, Permission.PIPELINE_DELETE | Permission.SECRET_READ | Permission.DEPLOY_PROD),
    (Role.ADMIN, Permission.SECRET_WRITE | Permission.PLUGIN_MANAGE | Permission.ADMIN),
]


def _permissions_for(role: Role) -> Permission:
    """Union of permissions for ``role`` and every lesser role."""
    granted = Permission(0)
    for candidate, extra in _ROLE_LADDER:
        granted |= extra
        if candidate is role:
            break
    return granted


ROLE_PERMISSIONS: dict[Role, Permission] = {
    Role.VIEWER: _permissions_for(Role.VIEWER),
    Role.DEVELOPER: _permissions_for(Role.DEVELOPER),
    Role.MAINTAINER: _permissions_for(Role.MAINTAINER),
    Role.ADMIN: _permissions_for(Role.ADMIN),
}

PROTECTED_BRANCH_PATTERNS = ("main", "master", "release/*")


@dataclass(frozen=True)
class AccessRequest:
    """One authorization check to be evaluated."""

    actor: str
    role: Role
    permission: Permission
    resource: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Decision:
    """Result of evaluating an AccessRequest."""

    allowed: bool
    reason: str

    def __bool__(self) -> bool:
        return self.allowed


class PermissionDenied(PermissionError):
    """Raised by ``assert_access`` when a Decision is negative."""

    def __init__(self, decision: Decision) -> None:
        super().__init__(decision.reason)
        self.decision = decision


@dataclass
class RBACPolicy:
    """Evaluates access requests with branch/environment aware rules."""

    protected_branches: tuple[str, ...] = PROTECTED_BRANCH_PATTERNS
    prod_requires_admin: bool = True

    def _role_permissions(self, role: Role) -> Permission:
        return ROLE_PERMISSIONS[role]

    def authorize(self, request: AccessRequest) -> Decision:
        """Return the authorization Decision for ``request``."""
        granted = self._role_permissions(request.role)
        if request.permission is Permission.ADMIN:
            if Permission.ADMIN in granted:
                return Decision(True, f"{request.actor} is admin")
            return Decision(False, f"role '{request.role.value}' cannot administer")

        if not (granted & request.permission):
            return Decision(
                False,
                f"role '{request.role.value}' lacks {request.permission!r} on "
                f"{request.resource.get('pipeline', 'resource')}",
            )

        environment = str(request.resource.get("environment", ""))
        branch = str(request.resource.get("branch", ""))
        if request.permission is Permission.DEPLOY_PROD or environment == "production":
            if self.prod_requires_admin and request.role is not Role.ADMIN:
                return Decision(False, "production deployments require the admin role")
        if branch and any(fnmatch.fnmatch(branch, pattern) for pattern in self.protected_branches):
            if request.permission in (Permission.PIPELINE_EDIT, Permission.PIPELINE_DELETE):
                if request.role not in (Role.MAINTAINER, Role.ADMIN):
                    return Decision(False, f"branch '{branch}' is protected; maintainer+ required")
        return Decision(True, "allowed by role permissions")

    def assert_access(self, request: AccessRequest) -> Decision:
        """Like :meth:`authorize` but raises on denial."""
        decision = self.authorize(request)
        if not decision:
            raise PermissionDenied(decision)
        return decision


@dataclass(frozen=True)
class AuditEntry:
    """One immutable audit record."""

    timestamp: float
    actor: str
    action: str
    resource: str
    outcome: str
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly representation."""
        return {
            "timestamp": self.timestamp,
            "actor": self.actor,
            "action": self.action,
            "resource": self.resource,
            "outcome": self.outcome,
            "detail": self.detail,
        }


class AuditLog:
    """Append-only audit trail with optional JSONL persistence."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else None
        self._entries: list[AuditEntry] = []
        self._lock = threading.Lock()

    def log(
        self,
        actor: str,
        action: str,
        resource: str,
        *,
        allowed: bool,
        detail: str = "",
    ) -> AuditEntry:
        """Record one event and persist it when a path is configured."""
        entry = AuditEntry(
            timestamp=time.time(),
            actor=actor,
            action=action,
            resource=resource,
            outcome="allow" if allowed else "deny",
            detail=detail,
        )
        with self._lock:
            self._entries.append(entry)
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry.to_dict()) + "\n")
        return entry

    @property
    def entries(self) -> list[AuditEntry]:
        """In-memory snapshot of all entries."""
        with self._lock:
            return list(self._entries)

    def query(
        self,
        *,
        actor: str | None = None,
        action: str | None = None,
        outcome: str | None = None,
        limit: int = 100,
    ) -> list[AuditEntry]:
        """Filter entries; most recent first."""
        selected = [
            entry
            for entry in reversed(self.entries)
            if (actor is None or entry.actor == actor)
            and (action is None or entry.action == action)
            and (outcome is None or entry.outcome == outcome)
        ]
        return selected[:limit]
