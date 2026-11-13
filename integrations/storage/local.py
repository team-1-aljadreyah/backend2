"""Local filesystem storage for development & testing."""
from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import quote

from django.conf import settings
from django.core.signing import TimestampSigner

from .base import StorageBackend


def _safe_join(root: Path, bucket: str, key: str) -> Path:
    base = (root / bucket).resolve()
    target = (base / key).resolve()
    if target != base and base not in target.parents:
        raise ValueError("Unsafe storage path traversal detected.")
    return target


class LocalMediaStorage(StorageBackend):
    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root or settings.MEDIA_ROOT).resolve()

    def put_bytes(self, bucket: str, key: str, content: bytes, content_type: str) -> str:
        path = _safe_join(self.root, bucket, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return key

    def delete(self, bucket: str, key: str) -> None:
        if not key:
            return
        path = _safe_join(self.root, bucket, key)
        if path.is_file():
            path.unlink()

    def get_url(
        self,
        bucket: str,
        key: str,
        *,
        request: Any = None,
        private: bool = False,
        expires_in: int = 900,
    ) -> str:
        if private:
            signer = TimestampSigner(salt="local-private-media")
            token = signer.sign(f"{bucket}/{key}")
            rel = f"/api/v1/media/signed?token={quote(token, safe='')}"
        else:
            media_prefix = getattr(settings, "MEDIA_URL", "/media/").rstrip("/")
            rel = f"{media_prefix}/{bucket}/{key.lstrip('/')}"
        if request is not None:
            return request.build_absolute_uri(rel)
        return rel
