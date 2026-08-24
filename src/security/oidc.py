"""OIDC authentication: token exchange and JWT validation.

Validates ID/access tokens against a configured issuer using either a static
public key PEM (air-gapped/test setups) or a JWKS endpoint fetched over HTTP.
Claims such as ``exp``, ``iss`` and ``aud`` are enforced, with typed errors
for each failure mode.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any

import jwt
from jwt import PyJWTError

LOG = logging.getLogger("devopspipeline.security.oidc")

Claims = dict[str, Any]
JWKS_CACHE_SECONDS = 3600


class InvalidTokenError(ValueError):
    """Base class for token validation failures."""


class MalformedTokenError(InvalidTokenError):
    """The token is not a well-formed compact JWS."""


class ExpiredTokenError(InvalidTokenError):
    """The token's ``exp`` claim has passed."""


class AudienceMismatchError(InvalidTokenError):
    """The ``aud`` claim does not include our client id."""


class SignatureInvalidError(InvalidTokenError):
    """Signature verification failed or key could not be found."""


@dataclass
class OIDCConfig:
    """Connection parameters for an OIDC provider."""

    issuer: str
    audience: str
    jwks_uri: str | None = None
    public_key_pem: str | None = None
    algorithms: tuple[str, ...] = ("RS256",)
    leeway_seconds: int = 30
    expected_claims: tuple[str, ...] = ("exp", "iat", "sub")


class OIDCValidator:
    """Validates JWTs issued by the configured provider."""

    def __init__(self, config: OIDCConfig) -> None:
        if not config.public_key_pem and not config.jwks_uri:
            raise ValueError("OIDCConfig requires public_key_pem or jwks_uri")
        self.config = config
        self._jwks_cache: dict[str, Any] = {}
        self._jwks_fetched_at: float = 0.0

    # -- keys ---------------------------------------------------------------
    def _fetch_jwks(self) -> dict[str, Any]:
        """Fetch (and cache) the provider JWKS document."""
        now = time.monotonic()
        if self._jwks_cache and now - self._jwks_fetched_at < JWKS_CACHE_SECONDS:
            return self._jwks_cache
        import httpx  # deferred optional dependency

        response = httpx.get(self.config.jwks_uri or "", timeout=10.0)
        response.raise_for_status()
        self._jwks_cache = response.json()
        self._jwks_fetched_at = now
        return self._jwks_cache

    def _resolve_key(self, header: dict[str, Any]) -> Any:
        """Return the verification key for the token header."""
        if self.config.public_key_pem is not None:
            return self.config.public_key_pem
        kid = header.get("kid")
        jwks = self._fetch_jwks()
        for key in jwks.get("keys", []):
            if key.get("kid") == kid:
                from jwt.algorithms import RSAAlgorithm

                return RSAAlgorithm.from_jwk(json.dumps(key))
        raise SignatureInvalidError(f"no JWKS entry for kid={kid!r}")

    @staticmethod
    def _decode_header(token: str) -> dict[str, Any]:
        """Read the JOSE header without verifying."""
        try:
            return jwt.get_unverified_header(token)
        except PyJWTError as exc:
            raise MalformedTokenError(str(exc)) from exc

    def _precheck(self, claims: Claims) -> None:
        """Fail fast on expiry/audience with precise error types."""
        expires_at = claims.get("exp")
        if isinstance(expires_at, (int, float)) and time.time() > expires_at + self.config.leeway_seconds:
            raise ExpiredTokenError("token expired")
        audience = claims.get("aud")
        audiences = audience.split() if isinstance(audience, str) else audience
        if audiences and self.config.audience not in [str(a) for a in audiences]:
            raise AudienceMismatchError(
                f"token audience {audiences!r} does not include {self.config.audience!r}"
            )

    # -- validation -----------------------------------------------------------
    def validate(self, token: str) -> Claims:
        """Verify signature and standard claims; returns decoded claims."""
        header = self._decode_header(token)
        if header.get("alg") not in self.config.algorithms:
            raise SignatureInvalidError(f"algorithm {header.get('alg')!r} not allowed")
        key = self._resolve_key(header)
        try:
            claims = jwt.decode(
                token,
                key=key,
                algorithms=list(self.config.algorithms),
                audience=self.config.audience,
                issuer=self.config.issuer,
                leeway=self.config.leeway_seconds,
                options={"require": list(self.config.expected_claims)},
            )
        except jwt.ExpiredSignatureError as exc:
            raise ExpiredTokenError(str(exc)) from exc
        except jwt.InvalidAudienceError as exc:
            raise AudienceMismatchError(str(exc)) from exc
        except PyJWTError as exc:
            raise SignatureInvalidError(str(exc)) from exc
        self._precheck(claims)
        return claims

    # -- flows -----------------------------------------------------------------
    def exchange_code(self, code: str, redirect_uri: str, *, client_id: str, client_secret: str) -> Claims:
        """Swap an authorization code for tokens at the token endpoint."""
        import httpx  # deferred optional dependency

        response = httpx.post(
            f"{self.config.issuer}/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": client_id,
                "client_secret": client_secret,
            },
            timeout=15.0,
        )
        if response.status_code >= 400:
            raise InvalidTokenError(f"code exchange failed: {response.status_code}")
        payload = response.json()
        return {"access_token": payload.get("access_token"), **payload}

    @staticmethod
    def has_scope(claims: Claims, scope: str) -> bool:
        """Check a space- or list-encoded scope claim."""
        raw = claims.get("scope") or claims.get("scp") or []
        scopes = set(raw.split()) if isinstance(raw, str) else set(map(str, raw))
        return scope in scopes


def build_well_known_uri(issuer: str) -> str:
    """Standard discovery document location for an issuer."""
    return f"{issuer.rstrip('/')}/.well-known/openid-configuration"
