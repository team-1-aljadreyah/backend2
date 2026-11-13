"""Middleware for Accept-Language detection and request ID tracking."""
from __future__ import annotations

import uuid
from collections.abc import Callable

from django.http import HttpRequest, HttpResponse

from apps.common.rls import clear_pg_session_context


class RequestLanguageAndAuditMiddleware:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        clear_pg_session_context()
        header = request.META.get("HTTP_ACCEPT_LANGUAGE", "") or ""
        request.lang = "en" if header.strip().lower().startswith("en") else "ar"  # type: ignore[attr-defined]
        request.request_id = request.META.get("HTTP_X_REQUEST_ID") or uuid.uuid4().hex[:12]  # type: ignore[attr-defined]

        try:
            response = self.get_response(request)
        finally:
            clear_pg_session_context()
        response["X-Request-ID"] = request.request_id  # type: ignore[attr-defined]
        return response
