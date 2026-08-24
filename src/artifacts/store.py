"""Artifact storage backends with integrity tracking.

Artifacts are addressed by a string key and carry SHA-256 checksums plus
sizes. The local filesystem store writes ``<key>.meta.json`` sidecars so
listings survive process restarts; the S3 store lazily imports boto3.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

LOG = logging.getLogger("devopspipeline.artifacts")

CHUNK_SIZE = 1024 * 1024


class ArtifactNotFoundError(KeyError):
    """Raised when an artifact key is missing from the store."""


def sha256_of(path: Path) -> str:
    """Stream a file through SHA-256 without loading it into memory."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class Artifact:
    """Metadata describing one stored artifact."""

    key: str
    size: int
    sha256: str
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    metadata: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly representation."""
        return asdict(self)


class ArtifactStore(ABC):
    """Interface implemented by every artifact backend."""

    @abstractmethod
    def put(self, local_path: str | Path, key: str, *, metadata: dict[str, str] | None = None) -> Artifact:
        """Upload/copy a local file under ``key``."""

    @abstractmethod
    def get(self, key: str, destination: str | Path) -> Path:
        """Download/copy artifact ``key`` to a local path."""

    @abstractmethod
    def delete(self, key: str) -> bool:
        """Remove an artifact; False when absent."""

    @abstractmethod
    def exists(self, key: str) -> bool:
        """Whether the key is present in the store."""

    @abstractmethod
    def list(self, prefix: str = "") -> list[Artifact]:
        """All artifacts whose key starts with ``prefix``."""

    def url(self, key: str, expires_in: int = 3600) -> Optional[str]:
        """Optional pre-signed/HTTP URL; None when unsupported."""
        return None

    def __iter__(self) -> Iterator[Artifact]:
        return iter(self.list())


class LocalArtifactStore(ArtifactStore):
    """Filesystem-backed store rooted at a directory."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _blob_path(self, key: str) -> Path:
        safe = key.replace("\\", "/").lstrip("/")
        path = (self.root / safe).resolve()
        if not str(path).startswith(str(self.root)):
            raise ValueError(f"artifact key escapes store root: {key}")
        return path

    def _meta_path(self, key: str) -> Path:
        return self._blob_path(key).with_suffix(self._blob_path(key).suffix + ".meta.json")

    def put(self, local_path: str | Path, key: str, *, metadata: dict[str, str] | None = None) -> Artifact:
        source = Path(local_path)
        if not source.is_file():
            raise FileNotFoundError(source)
        blob = self._blob_path(key)
        blob.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, blob)
        artifact = Artifact(
            key=key,
            size=blob.stat().st_size,
            sha256=sha256_of(blob),
            metadata=dict(metadata or {}),
        )
        self._meta_path(key).write_text(json.dumps(artifact.to_dict(), indent=2), encoding="utf-8")
        LOG.debug("stored artifact %s (%d bytes)", key, artifact.size)
        return artifact

    def get(self, key: str, destination: str | Path) -> Path:
        blob = self._blob_path(key)
        if not blob.is_file():
            raise ArtifactNotFoundError(key)
        target = Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(blob, target)
        return target

    def delete(self, key: str) -> bool:
        blob = self._blob_path(key)
        meta = self._meta_path(key)
        existed = blob.exists()
        blob.unlink(missing_ok=True)
        meta.unlink(missing_ok=True)
        return existed

    def exists(self, key: str) -> bool:
        return self._blob_path(key).is_file()

    def list(self, prefix: str = "") -> list[Artifact]:
        artifacts: list[Artifact] = []
        for meta_file in sorted(self.root.rglob("*.meta.json")):
            relative = meta_file.relative_to(self.root).as_posix()
            key = relative.removesuffix(".meta.json")
            if prefix and not key.startswith(prefix):
                continue
            try:
                data = json.loads(meta_file.read_text(encoding="utf-8"))
                artifacts.append(Artifact(**data))
            except (json.JSONDecodeError, TypeError):
                LOG.warning("skipping corrupt artifact metadata %s", relative)
        return artifacts

    def url(self, key: str, expires_in: int = 3600) -> Optional[str]:  # noqa: ARG002
        blob = self._blob_path(key)
        return blob.as_uri() if blob.exists() else None


class S3ArtifactStore(ArtifactStore):
    """AWS S3-backed store; requires the optional ``boto3`` extra."""

    def __init__(self, bucket: str, prefix: str = "artifacts", region_name: str | None = None) -> None:
        try:
            import boto3  # deferred optional dependency
        except ImportError as exc:
            raise RuntimeError("S3ArtifactStore requires 'pip install devopspipeline[aws]'") from exc
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self._client = boto3.client("s3", region_name=region_name)

    def _full_key(self, key: str) -> str:
        return f"{self.prefix}/{key.lstrip('/')}" if self.prefix else key

    def put(self, local_path: str | Path, key: str, *, metadata: dict[str, str] | None = None) -> Artifact:
        source = Path(local_path)
        full_key = self._full_key(key)
        self._client.upload_file(str(source), self.bucket, full_key)
        head = self._client.head_object(Bucket=self.bucket, Key=full_key)
        artifact = Artifact(
            key=key,
            size=int(head.get("ContentLength", source.stat().st_size)),
            sha256=str(head.get("ETag", "").strip('"')),
            metadata={k: v for k, v in (metadata or {}).items()},
        )
        return artifact

    def get(self, key: str, destination: str | Path) -> Path:
        target = Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        self._client.download_file(self.bucket, self._full_key(key), str(target))
        return target

    def delete(self, key: str) -> bool:
        existed = self.exists(key)
        self._client.delete_object(Bucket=self.bucket, Key=self._full_key(key))
        return existed

    def exists(self, key: str) -> bool:
        from botocore.exceptions import ClientError

        try:
            self._client.head_object(Bucket=self.bucket, Key=self._full_key(key))
            return True
        except ClientError:
            return False

    def list(self, prefix: str = "") -> list[Artifact]:
        full_prefix = self._full_key(prefix)
        response = self._client.list_objects_v2(
            Bucket=self.bucket, Prefix=f"{full_prefix}/" if prefix else f"{self.prefix}/"
        )
        artifacts: list[Artifact] = []
        for item in response.get("Contents", []):
            stripped = item["Key"].removeprefix(f"{self.prefix}/")
            artifacts.append(
                Artifact(key=stripped, size=int(item["Size"]), sha256=str(item.get("ETag", "")).strip('"'))
            )
        return artifacts

    def url(self, key: str, expires_in: int = 3600) -> Optional[str]:
        return self._client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": self._full_key(key)},
            ExpiresIn=expires_in,
        )
