"""Tests for the security subsystem: vault, OIDC and RBAC policies."""

from __future__ import annotations

import json
import time

import jwt
import pytest

from src.security.oidc import (
    AudienceMismatchError,
    ExpiredTokenError,
    MalformedTokenError,
    OIDCValidator,
    SignatureInvalidError,
)
from src.security.policies import (
    AuditLog,
    Decision,
    Permission,
    PermissionDenied,
    Role,
)
from src.security.policies import AccessRequest, RBACPolicy
from src.security.vault import SecretVault, VaultError


# --------------------------------------------------------------------------- #
# vault
# --------------------------------------------------------------------------- #
def test_vault_roundtrip(vault: SecretVault) -> None:
    vault.set_secret("API_TOKEN", "s3cr3t-value")
    assert vault.get_secret("API_TOKEN") == "s3cr3t-value"
    assert vault.list_secrets() == ["api_token"] or vault.list_secrets() == ["API_TOKEN"]


def test_vault_persists_encrypted(tmp_path: object, vault: SecretVault) -> None:
    from pathlib import Path

    vault.set_secret("DB_PASSWORD", "hunter2!")
    vault.save()
    store_file = Path(str(tmp_path)) / "vault.json"
    raw = store_file.read_text(encoding="utf-8")
    assert "hunter2!" not in raw  # ciphertext at rest
    payload = json.loads(raw)
    assert set(payload["secrets"]) == {"DB_PASSWORD"}

    reloaded = SecretVault(password="correct-horse-battery-staple", store_path=store_file)
    assert reloaded.get_secret("DB_PASSWORD") == "hunter2!"


def test_vault_wrong_password_fails(tmp_path: object) -> None:
    from pathlib import Path

    path = Path(str(tmp_path)) / "vault.json"
    first = SecretVault(password="right", store_path=path)
    first.set_secret("k", "v")
    first.save()
    second = SecretVault(password="wrong", salt=b"devopspipeline-default-salt", store_path=path)
    with pytest.raises(VaultError):
        second.get_secret("k")


def test_vault_masking() -> None:
    assert SecretVault.mask("supersecret") == "*" * 8 + "ret"
    assert SecretVault.mask("abc") == "***"


def test_vault_inject_environment(vault: SecretVault) -> None:
    vault.set_secret("token", "abc123")
    injected = vault.inject_environment({"PIPELINE_TOKEN": "token"})
    assert injected == {"PIPELINE_TOKEN": "abc123"}


def test_vault_requires_key_or_password() -> None:
    with pytest.raises(ValueError):
        SecretVault()


# --------------------------------------------------------------------------- #
# rbac / audit
# --------------------------------------------------------------------------- #
@pytest.fixture()
def policy() -> RBACPolicy:
    return RBACPolicy()


def test_viewer_cannot_edit(policy: RBACPolicy) -> None:
    decision = policy.authorize(
        AccessRequest(actor="ana", role=Role.VIEWER, permission=Permission.PIPELINE_EDIT, resource={"pipeline": "web"})
    )
    assert not decision


def test_developer_can_run_and_edit(policy: RBACPolicy) -> None:
    for permission in (Permission.PIPELINE_RUN, Permission.PIPELINE_EDIT):
        assert policy.assert_access(
            AccessRequest(actor="dev", role=Role.DEVELOPER, permission=permission, resource={"pipeline": "web"})
        )


def test_production_deploy_requires_admin(policy: RBACPolicy) -> None:
    request = AccessRequest(
        actor="maint",
        role=Role.MAINTAINER,
        permission=Permission.DEPLOY_PROD,
        resource={"environment": "production"},
    )
    assert not policy.authorize(request)
    admin_ok = policy.authorize(
        AccessRequest(
            actor="boss", role=Role.ADMIN, permission=Permission.DEPLOY_PROD, resource={"environment": "production"}
        )
    )
    assert admin_ok.allowed


def test_protected_branch_needs_maintainer(policy: RBACPolicy) -> None:
    request = AccessRequest(
        actor="dev", role=Role.DEVELOPER, permission=Permission.PIPELINE_EDIT, resource={"branch": "main"}
    )
    with pytest.raises(PermissionDenied):
        policy.assert_access(request)


def test_release_wildcard_is_protected(policy: RBACPolicy) -> None:
    request = AccessRequest(
        actor="dev", role=Role.DEVELOPER, permission=Permission.PIPELINE_DELETE, resource={"branch": "release/9.9"}
    )
    decision: Decision = policy.authorize(request)
    assert not decision.allowed


def test_audit_log_records_and_queries(tmp_path: object) -> None:
    from pathlib import Path

    log = AuditLog(Path(str(tmp_path)) / "audit.jsonl")
    log.log("ana", "pipeline.run", "web", allowed=True)
    log.log("bob", "secret.read", "vault", allowed=False)
    denied = log.query(outcome="deny")
    assert len(denied) == 1 and denied[0].actor == "bob"
    lines = (Path(str(tmp_path)) / "audit.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["action"] == "pipeline.run"


# --------------------------------------------------------------------------- #
# oidc
# --------------------------------------------------------------------------- #
def _make_token(validator: OIDCValidator, claims: dict, *, private_pem: str) -> str:
    return jwt.encode(claims, private_pem, algorithm="RS256")


def base_claims() -> dict:
    now = int(time.time())
    return {
        "iss": "https://auth.example.com",
        "aud": "devops-ci",
        "sub": "user-123",
        "iat": now - 10,
        "exp": now + 600,
        "scope": "pipeline:run pipeline:edit",
    }


def test_oidc_valid_token_decodes(oidc_validator: OIDCValidator) -> None:
    token = _make_token(
        oidc_validator, base_claims(), private_pem=oidc_validator._test_private_pem  # type: ignore[attr-defined]
    )
    claims = oidc_validator.validate(token)
    assert claims["sub"] == "user-123"


def test_oidc_expired_token_rejected(oidc_validator: OIDCValidator) -> None:
    claims = base_claims()
    claims["exp"] = int(time.time()) - 3600
    token = jwt.encode(claims, oidc_validator._test_private_pem, algorithm="RS256")  # type: ignore[attr-defined]
    with pytest.raises(ExpiredTokenError):
        oidc_validator.validate(token)


def test_oidc_wrong_audience_rejected(oidc_validator: OIDCValidator) -> None:
    claims = base_claims()
    claims["aud"] = "someone-else"
    token = jwt.encode(claims, oidc_validator._test_private_pem, algorithm="RS256")  # type: ignore[attr-defined]
    with pytest.raises(AudienceMismatchError):
        oidc_validator.validate(token)


def test_oidc_tampered_token_rejected(oidc_validator: OIDCValidator) -> None:
    token = jwt.encode(base_claims(), oidc_validator._test_private_pem, algorithm="RS256")  # type: ignore[attr-defined]
    header, body, signature = token.split(".")
    tampered_body = body[:-2] + ("AA" if body[-2:] != "AA" else "BB")
    with pytest.raises(SignatureInvalidError):
        oidc_validator.validate(f"{header}.{tampered_body}.{signature}")


def test_oidc_malformed_token_rejected(oidc_validator: OIDCValidator) -> None:
    with pytest.raises(MalformedTokenError):
        oidc_validator.validate("not-a-jwt")


def test_oidc_scope_helper() -> None:
    assert OIDCValidator.has_scope({"scope": "pipeline:run other"}, "pipeline:run")
    assert not OIDCValidator.has_scope({"scp": ["a", "b"]}, "pipeline:run")
