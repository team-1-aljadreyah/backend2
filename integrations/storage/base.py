"""Abstract object storage contract."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class StorageBackend(ABC):
    @abstractmethod
    def put_bytes(self, bucket: str, key: str, content: bytes, content_type: str) -> str:
        """Persist bytes under (bucket, key) and return stored object key."""

    @abstractmethod
    def delete(self, bucket: str, key: str) -> None:
        """Remove object if present."""

    @abstractmethod
    def get_url(
        self,
        bucket: str,
        key: str,
        *,
        request: Any = None,
        private: bool = False,
        expires_in: int = 900,
    ) -> str:
        """Return reachable public or short-lived signed URL for frontend consumption."""
