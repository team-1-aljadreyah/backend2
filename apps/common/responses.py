"""Standard response envelope helpers matching frontend/lib/api/axios.ts."""
from __future__ import annotations

from typing import Any

from rest_framework import status as http_status
from rest_framework.response import Response


def ok_response(
    data: Any = None,
    message: str | None = None,
    status: int = http_status.HTTP_200_OK,
) -> Response:
    payload: dict[str, Any] = {
        "success": True,
        "data": data,
    }
    if message is not None:
        payload["message"] = message
    return Response(payload, status=status)


def created_response(data: Any = None, message: str | None = None) -> Response:
    return ok_response(data=data, message=message, status=http_status.HTTP_201_CREATED)
