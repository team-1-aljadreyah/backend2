"""Typed error codes matching frontend/lib/api/errors.ts and bilingual exception handling."""
from __future__ import annotations

from typing import Any

from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.http import Http404
from rest_framework import status as http_status
from rest_framework.exceptions import (
    AuthenticationFailed,
    NotAuthenticated,
    NotFound,
    PermissionDenied,
    Throttled,
    ValidationError,
)
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler


class ErrorCode:
    VALIDATION = "VALIDATION"
    UNAUTHENTICATED = "UNAUTHENTICATED"
    FORBIDDEN = "FORBIDDEN"
    NOT_FOUND = "NOT_FOUND"
    OUT_OF_STOCK = "OUT_OF_STOCK"
    DEBT_LIMIT = "DEBT_LIMIT"
    MIN_ORDER = "MIN_ORDER"
    EDIT_CLOSED = "EDIT_CLOSED"
    RETURN_TOO_MUCH = "RETURN_TOO_MUCH"
    ALREADY_REVERSED = "ALREADY_REVERSED"
    MONTH_CLOSED = "MONTH_CLOSED"
    BAD_STATE = "BAD_STATE"
    RATE_LIMITED = "RATE_LIMITED"
    MAINTENANCE = "MAINTENANCE"
    FEATURE_OFF = "FEATURE_OFF"
    SIGNUP_CLOSED = "SIGNUP_CLOSED"
    TOO_OLD = "TOO_OLD"
    LOCKED = "LOCKED"
    NETWORK = "NETWORK"


DEFAULT_CODE_MESSAGES: dict[str, tuple[str, str]] = {
    ErrorCode.VALIDATION: ("البيانات المدخلة غير صحيحة", "Invalid input data"),
    ErrorCode.UNAUTHENTICATED: ("يجب تسجيل الدخول أولاً", "Authentication required"),
    ErrorCode.FORBIDDEN: ("ليست لديك صلاحية لتنفيذ هذا الإجراء", "You do not have permission to perform this action"),
    ErrorCode.NOT_FOUND: ("العنصر المطلوب غير موجود", "Requested resource not found"),
    ErrorCode.OUT_OF_STOCK: ("المخزون المتوفر غير كافٍ", "Insufficient available stock"),
    ErrorCode.DEBT_LIMIT: ("تجاوز الطلب الحد الائتماني المسموح للعميل", "Customer debt limit exceeded"),
    ErrorCode.MIN_ORDER: ("الطلب أقل من الحد الأدنى المسموح", "Order is below the minimum amount"),
    ErrorCode.EDIT_CLOSED: ("انتهى وقت تعديل هذا الطلب", "The edit window for this order has closed"),
    ErrorCode.RETURN_TOO_MUCH: ("الكمية المرتجعة تتجاوز الكمية القابلة للإرجاع", "Return quantity exceeds remaining deliverable quantity"),
    ErrorCode.ALREADY_REVERSED: ("تم عكس هذه الدفعة مسبقاً", "This payment has already been reversed"),
    ErrorCode.MONTH_CLOSED: ("الشهر المالي مغلق ولا يمكن تعديله", "Commission month is closed"),
    ErrorCode.BAD_STATE: ("الحالة الحالية لا تسمح بهذا الإجراء", "Operation not allowed in the current state"),
    ErrorCode.RATE_LIMITED: ("محاولات كثيرة، حاول لاحقاً", "Too many requests, please try again later"),
    ErrorCode.MAINTENANCE: ("التطبيق في صيانة الآن", "The app is under maintenance"),
    ErrorCode.FEATURE_OFF: ("هذه الميزة متوقفة الآن", "This feature is turned off"),
    ErrorCode.SIGNUP_CLOSED: ("تسجيل الحسابات الجديدة متوقف حالياً", "New account registration is turned off"),
    ErrorCode.TOO_OLD: ("مرت المدة المسموح فيها بهذا الإجراء", "The allowed window for this action has passed"),
    ErrorCode.LOCKED: ("الحساب مقفل مؤقتاً بسبب محاولات دخول خاطئة", "Account temporarily locked due to failed logins"),
}


def get_request_lang(request: Any) -> str:
    if request is None:
        return "ar"
    lang = getattr(request, "lang", None)
    if lang in ("ar", "en"):
        return lang
    header = ""
    if hasattr(request, "headers"):
        header = request.headers.get("Accept-Language", "") or ""
    elif hasattr(request, "META"):
        header = request.META.get("HTTP_ACCEPT_LANGUAGE", "") or ""
    if header.strip().lower().startswith("en"):
        return "en"
    return "ar"


def localize(msg: str | tuple[str, str], lang: str) -> str:
    if isinstance(msg, tuple) and len(msg) == 2:
        return msg[1] if lang == "en" else msg[0]
    return str(msg)


class ApiException(Exception):
    """Domain/API exception serialized to frontend-compatible error envelope."""

    def __init__(
        self,
        code: str = ErrorCode.VALIDATION,
        message: str | tuple[str, str] | None = None,
        status_code: int = http_status.HTTP_400_BAD_REQUEST,
        errors: dict[str, str | tuple[str, str]] | None = None,
    ) -> None:
        self.code = code
        self.message = message or DEFAULT_CODE_MESSAGES.get(code, DEFAULT_CODE_MESSAGES[ErrorCode.VALIDATION])
        self.status_code = status_code
        self.errors = errors or {}
        super().__init__(localize(self.message, "en"))


def _flatten_drf_errors(detail: Any, lang: str) -> tuple[str, dict[str, str]]:
    errors: dict[str, str] = {}
    if isinstance(detail, dict):
        for field, val in detail.items():
            if isinstance(val, list) and val:
                errors[str(field)] = str(val[0])
            elif isinstance(val, dict):
                _, nested = _flatten_drf_errors(val, lang)
                for k, v in nested.items():
                    errors[f"{field}.{k}"] = v
            else:
                errors[str(field)] = str(val)
        first_msg = next(iter(errors.values()), localize(DEFAULT_CODE_MESSAGES[ErrorCode.VALIDATION], lang))
        return first_msg, errors
    if isinstance(detail, list) and detail:
        return str(detail[0]), {}
    return str(detail), {}


def custom_exception_handler(exc: Exception, context: dict[str, Any]) -> Response | None:
    request = context.get("request")
    lang = get_request_lang(request)

    if isinstance(exc, ApiException):
        localized_errors = {k: localize(v, lang) for k, v in exc.errors.items()} if exc.errors else None
        payload: dict[str, Any] = {
            "success": False,
            "code": exc.code,
            "message": localize(exc.message, lang),
        }
        if localized_errors:
            payload["errors"] = localized_errors
        return Response(payload, status=exc.status_code)

    if isinstance(exc, (NotAuthenticated, AuthenticationFailed)):
        return Response(
            {
                "success": False,
                "code": ErrorCode.UNAUTHENTICATED,
                "message": localize(DEFAULT_CODE_MESSAGES[ErrorCode.UNAUTHENTICATED], lang),
            },
            status=http_status.HTTP_401_UNAUTHORIZED,
        )

    if isinstance(exc, (PermissionDenied, DjangoPermissionDenied)):
        return Response(
            {
                "success": False,
                "code": ErrorCode.FORBIDDEN,
                "message": localize(DEFAULT_CODE_MESSAGES[ErrorCode.FORBIDDEN], lang),
            },
            status=http_status.HTTP_403_FORBIDDEN,
        )

    if isinstance(exc, (NotFound, Http404)):
        return Response(
            {
                "success": False,
                "code": ErrorCode.NOT_FOUND,
                "message": localize(DEFAULT_CODE_MESSAGES[ErrorCode.NOT_FOUND], lang),
            },
            status=http_status.HTTP_404_NOT_FOUND,
        )

    if isinstance(exc, ValidationError):
        first_msg, field_errors = _flatten_drf_errors(exc.detail, lang)
        payload = {
            "success": False,
            "code": ErrorCode.VALIDATION,
            "message": first_msg,
        }
        if field_errors:
            payload["errors"] = field_errors
        return Response(payload, status=http_status.HTTP_400_BAD_REQUEST)

    if isinstance(exc, Throttled):
        msg = (
            f"تم تجاوز حد المحاولات المسموح. يرجى المحاولة بعد {exc.wait or 60} ثانية"
            if lang == "ar"
            else f"Too many requests. Please try again in {exc.wait or 60} seconds."
        )
        return Response(
            {
                "success": False,
                "code": ErrorCode.VALIDATION,
                "message": msg,
            },
            status=http_status.HTTP_429_TOO_MANY_REQUESTS,
        )

    response = drf_exception_handler(exc, context)
    if response is not None:
        first_msg, field_errors = _flatten_drf_errors(response.data, lang)
        payload = {
            "success": False,
            "code": ErrorCode.BAD_STATE if response.status_code == 409 else ErrorCode.VALIDATION,
            "message": first_msg,
        }
        if field_errors:
            payload["errors"] = field_errors
        return Response(payload, status=response.status_code)

    return None
