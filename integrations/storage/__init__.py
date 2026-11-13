"""Pluggable object storage backend (local dev vs Supabase Storage production)."""
from __future__ import annotations

from django.conf import settings

from .base import StorageBackend
from .local import LocalMediaStorage
from .supabase import SupabaseStorageClient


def get_storage() -> StorageBackend:
    backend = getattr(settings, "STORAGE_BACKEND", "local").lower()
    if backend == "supabase":
        return SupabaseStorageClient()
    return LocalMediaStorage()


__all__ = ["StorageBackend", "LocalMediaStorage", "SupabaseStorageClient", "get_storage"]
