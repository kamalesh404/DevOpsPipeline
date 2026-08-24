"""Encrypted secret vault.

Secrets are encrypted at rest with Fernet (AES-128-CBC + HMAC). Master keys
can be provided directly or derived from a password via PBKDF2-HMAC-SHA256.
The vault persists to a JSON file whose values are ciphertext tokens, keeps a
TTL-bounded in-memory cache for hot reads, and supports masking for logs.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import threading
import time
from pathlib import Path
from typing import Mapping

from cryptography.fernet import Fernet, InvalidToken

LOG = logging.getLogger("devopspipeline.security.vault")

PBKDF2_ITERATIONS = 480_000
CACHE_TTL_SECONDS = 300.0


class VaultError(RuntimeError):
    """Raised on vault integrity/availability problems."""


def generate_key() -> bytes:
    """Generate a fresh Fernet-compatible master key."""
    return Fernet.generate_key()


def derive_key(password: str, salt: bytes) -> bytes:
    """Derive a Fernet key from ``password`` using PBKDF2-HMAC-SHA256."""
    raw = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return base64.urlsafe_b64encode(raw)


class SecretVault:
    """File-backed, encrypted-at-rest secret store."""

    def __init__(
        self,
        *,
        master_key: bytes | None = None,
        password: str | None = None,
        salt: bytes | None = None,
        store_path: str | Path | None = None,
    ) -> None:
        if master_key is None and password is None:
            raise ValueError("provide either master_key or password")
        self.salt = salt if salt is not None else (b"" if master_key else b"devopspipeline-default-salt")
        self._master_key = master_key if master_key is not None else derive_key(password or "", self.salt)
        self.store_path = Path(store_path) if store_path else None
        self._fernet = Fernet(self._master_key)
        self._secrets: dict[str, str] = {}
        self._cache: dict[str, tuple[str, float]] = {}
        self._lock = threading.RLock()
        if self.store_path is not None:
            self.load()

    # -- persistence ------------------------------------------------------
    def save(self) -> None:
        """Write the encrypted secret set to disk."""
        if self.store_path is None:
            raise VaultError("vault has no store_path configured")
        payload = {
            "salt": base64.b64encode(self.salt).decode("ascii"),
            "version": 1,
            "secrets": dict(self._secrets),
        }
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        self.store_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def load(self) -> int:
        """Load secrets from disk; returns how many were restored."""
        if self.store_path is None or not self.store_path.exists():
            return 0
        try:
            payload = json.loads(self.store_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise VaultError(f"corrupt vault file: {exc}") from exc
        stored_salt = base64.b64decode(payload.get("salt", ""))
        if stored_salt and self.salt and stored_salt != self.salt:
            raise VaultError("vault salt mismatch; wrong password?")
        with self._lock:
            self._secrets = {str(k): str(v) for k, v in payload.get("secrets", {}).items()}
            self._cache.clear()
        return len(self._secrets)

    def rotate_master_key(self, new_password: str, *, new_salt: bytes | None = None) -> None:
        """Decrypt everything and re-encrypt under a new derived key."""
        with self._lock:
            plaintext = {name: self.get_secret(name) for name in list(self._secrets)}
        self.salt = new_salt or b"devopspipeline-rotated-salt"
        self._master_key = derive_key(new_password, self.salt)
        self._fernet = Fernet(self._master_key)
        with self._lock:
            self._cache.clear()
            for name, value in plaintext.items():
                self._secrets[name] = self._fernet.encrypt(value.encode("utf-8")).decode("ascii")
        if self.store_path is not None:
            self.save()

    # -- crud --------------------------------------------------------------
    def set_secret(self, name: str, value: str) -> None:
        """Encrypt and store ``value`` under ``name``."""
        token = self._fernet.encrypt(value.encode("utf-8")).decode("ascii")
        with self._lock:
            self._secrets[name] = token
            self._cache[name] = (value, time.monotonic())

    def get_secret(self, name: str) -> str:
        """Decrypt and return ``name``'s value; raises KeyError when absent."""
        now = time.monotonic()
        cached = self._cache.get(name)
        if cached and now - cached[1] < CACHE_TTL_SECONDS:
            return cached[0]
        try:
            token = self._secrets[name]
        except KeyError as exc:
            raise KeyError(f"secret '{name}' not found") from exc
        try:
            value = self._fernet.decrypt(token.encode("ascii")).decode("utf-8")
        except InvalidToken as exc:
            raise VaultError(f"cannot decrypt '{name}'; wrong master key?") from exc
        with self._lock:
            self._cache[name] = (value, time.monotonic())
        return value

    def delete_secret(self, name: str) -> bool:
        """Remove a secret; returns False when it did not exist."""
        with self._lock:
            removed = self._secrets.pop(name, None)
            self._cache.pop(name, None)
            return removed is not None

    def list_secrets(self) -> list[str]:
        """Sorted secret names (never values)."""
        return sorted(self._secrets)

    def inject_environment(self, names: Mapping[str, str] | None = None) -> dict[str, str]:
        """Resolve secret names into an env-var mapping.

        ``names`` maps environment variable name -> secret name; when omitted,
        each secret is exposed under its own upper-cased name.
        """
        mapping = names or {name: name.upper() for name in self.list_secrets()}
        return {env_name: self.get_secret(secret_name) for env_name, secret_name in mapping.items()}

    # -- helpers -----------------------------------------------------------
    @staticmethod
    def mask(value: str, visible: int = 4) -> str:
        """Return a log-safe rendering showing only the last characters."""
        if len(value) <= visible:
            return "*" * len(value)
        return "*" * 8 + value[-visible:]

    def describe(self) -> dict[str, object]:
        """Non-sensitive metadata about this vault instance."""
        return {
            "secret_count": len(self._secrets),
            "persisted": self.store_path is not None,
            "store_path": str(self.store_path) if self.store_path else None,
        }
