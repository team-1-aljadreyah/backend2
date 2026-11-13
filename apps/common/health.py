"""Health check and protected metrics endpoints."""
from __future__ import annotations

import hmac
from typing import Any

from django.conf import settings
from django.db import connection
from rest_framework import status as http_status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.common.exceptions import ErrorCode


class LiveHealthView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request) -> Response:
        return Response({"status": "ok"}, status=http_status.HTTP_200_OK)


class ReadyHealthView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request) -> Response:
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1;")
                cursor.fetchone()
        except Exception:
            return Response(
                {"status": "unhealthy", "db": "error"},
                status=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        return Response(
            {"status": "ready", "db": "ok", "engine": connection.vendor},
            status=http_status.HTTP_200_OK,
        )


class MetricsView(APIView):
    permission_classes = [AllowAny]

    @staticmethod
    def _matches_metrics_token(request) -> bool:
        expected_token = getattr(settings, "METRICS_TOKEN", "")
        auth_header = request.headers.get("Authorization", "")
        if expected_token and auth_header.startswith("Bearer "):
            provided = auth_header[len("Bearer ") :].strip()
            return hmac.compare_digest(provided, expected_token)
        return False

    def perform_authentication(self, request) -> None:
        if self._matches_metrics_token(request):
            return
        super().perform_authentication(request)

    def _is_authorized(self, request) -> bool:
        if self._matches_metrics_token(request):
            return True
        user = getattr(request, "user", None)
        return bool(user and user.is_authenticated and getattr(user, "role", None) in ("manager", "accountant"))

    def get(self, request) -> Response:
        if not self._is_authorized(request):
            return Response(
                {"success": False, "code": ErrorCode.FORBIDDEN, "message": "Forbidden"},
                status=http_status.HTTP_403_FORBIDDEN,
            )
        data: dict[str, Any] = {
            "db_vendor": connection.vendor,
            "storage_backend": getattr(settings, "STORAGE_BACKEND", "local"),
            "qi_card_mode": getattr(settings, "QI_CARD_MODE", "disabled"),
        }
        return Response({"success": True, "data": data}, status=http_status.HTTP_200_OK)
