"""Idempotency-Key deduplication helper with canonical payload conflict checking."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import Any

from django.db import IntegrityError, transaction
from rest_framework import status as http_status
from rest_framework.response import Response

from apps.common.exceptions import ApiException, ErrorCode
from apps.common.models import IdempotencyKey


def _hash_payload(data: Any) -> str:
    canonical = json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def run_idempotent(
    *,
    request: Any,
    scope: str,
    fn: Callable[[], tuple[int, dict[str, Any]]],
    client_ref: str | None = None,
) -> Response:
    """
    If `Idempotency-Key` header or `client_ref` is present:
    - Replay cached `(status, response_body)` when payload hash matches.
    - Raise `BAD_STATE` (409) if the same key is reused with a different request payload.
    - Otherwise execute `fn()` inside atomic block and store result cleanly.
    """
    key = ""
    if hasattr(request, "headers"):
        key = (request.headers.get("Idempotency-Key") or "").strip()
    if not key and client_ref:
        key = str(client_ref).strip()
    if not key and isinstance(getattr(request, "data", None), dict):
        candidate = request.data.get("client_ref")
        if candidate:
            key = str(candidate).strip()
    if not key:
        status_code, body = fn()
        return Response(body, status=status_code)

    user_part = str(getattr(getattr(request, "user", None), "id", "anon"))
    full_scope = f"{scope}:{user_part}"
    req_hash = _hash_payload(getattr(request, "data", {}))

    existing = IdempotencyKey.objects.filter(scope=full_scope, key=key).first()
    if existing:
        if existing.request_hash != req_hash:
            raise ApiException(
                code=ErrorCode.BAD_STATE,
                message=("تم استخدام مفتاح التكرار لطلب مختلف مسبقاً", "Idempotency-Key reused with a different payload"),
                status_code=http_status.HTTP_409_CONFLICT,
            )
        return Response(existing.response_body, status=existing.response_status)

    with transaction.atomic():
        # Double check under transaction / unique constraint protection
        existing_locked = IdempotencyKey.objects.select_for_update().filter(scope=full_scope, key=key).first()
        if existing_locked:
            if existing_locked.request_hash != req_hash:
                raise ApiException(
                    code=ErrorCode.BAD_STATE,
                    message=("تم استخدام مفتاح التكرار لطلب مختلف مسبقاً", "Idempotency-Key reused with a different payload"),
                    status_code=http_status.HTTP_409_CONFLICT,
                )
            return Response(existing_locked.response_body, status=existing_locked.response_status)

        status_code, body = fn()
        try:
            IdempotencyKey.objects.create(
                scope=full_scope,
                key=key,
                request_hash=req_hash,
                response_status=status_code,
                response_body=body,
            )
        except IntegrityError:
            pass
        return Response(body, status=status_code)
