"""Supabase Storage HTTP client for staging and production."""
from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx
from django.conf import settings

from .base import StorageBackend


class SupabaseStorageError(RuntimeError):
    """Raised when Supabase Storage API rejects or fails an operation."""


class SupabaseStorageClient(StorageBackend):
    def __init__(
        self,
        base_url: str | None = None,
        service_role_key: str | None = None,
        timeout: float = 15.0,
    ) -> None:
        self.base_url = (base_url or getattr(settings, "SUPABASE_URL", "")).rstrip("/")
        self.service_role_key = service_role_key or getattr(settings, "SUPABASE_SERVICE_ROLE_KEY", "")
        if not self.base_url or not self.service_role_key:
            raise RuntimeError("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set when STORAGE_BACKEND=supabase.")
        self.timeout = timeout

    def _headers(self, content_type: str | None = None) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.service_role_key}",
            "apikey": self.service_role_key,
        }
        if content_type:
            headers["Content-Type"] = content_type
        return headers

    def put_bytes(self, bucket: str, key: str, content: bytes, content_type: str) -> str:
        clean_key = key.lstrip("/")
        url = f"{self.base_url}/storage/v1/object/{quote(bucket)}/{quote(clean_key, safe='/')}"
        headers = {**self._headers(content_type), "x-upsert": "true"}
        resp = httpx.post(url, headers=headers, content=content, timeout=self.timeout)
        if resp.status_code not in (200, 201):
            raise SupabaseStorageError(f"Supabase upload failed ({resp.status_code}): {resp.text[:300]}")
        return clean_key

    def delete(self, bucket: str, key: str) -> None:
        if not key:
            return
        clean_key = key.lstrip("/")
        url = f"{self.base_url}/storage/v1/object/{quote(bucket)}/{quote(clean_key, safe='/')}"
        resp = httpx.delete(url, headers=self._headers(), timeout=self.timeout)
        if resp.status_code not in (200, 204, 404):
            raise SupabaseStorageError(f"Supabase delete failed ({resp.status_code}): {resp.text[:300]}")

    def get_url(
        self,
        bucket: str,
        key: str,
        *,
        request: Any = None,
        private: bool = False,
        expires_in: int = 900,
    ) -> str:
        clean_key = key.lstrip("/")
        if not private:
            return f"{self.base_url}/storage/v1/object/public/{quote(bucket)}/{quote(clean_key, safe='/')}"

        url = f"{self.base_url}/storage/v1/object/sign/{quote(bucket)}/{quote(clean_key, safe='/')}"
        resp = httpx.post(url, headers=self._headers("application/json"), json={"expiresIn": expires_in}, timeout=self.timeout)
        if resp.status_code != 200:
            raise SupabaseStorageError(f"Supabase signed URL generation failed ({resp.status_code}): {resp.text[:300]}")
        data = resp.json()
        signed_path = data.get("signedURL") or data.get("signedUrl") or ""
        if not signed_path:
            raise SupabaseStorageError("Supabase signed URL missing signedURL field.")
        if signed_path.startswith("http"):
            return signed_path
        if not signed_path.startswith("/"):
            signed_path = "/" + signed_path
        if not signed_path.startswith("/storage/v1"):
            signed_path = "/storage/v1" + signed_path
        return f"{self.base_url}{signed_path}"
