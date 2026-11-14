"""Server-side Wevlix OTP API client (never exposes OTP codes or API keys to clients)."""
from __future__ import annotations

import logging
from typing import Any

import httpx
from django.conf import settings
from rest_framework import status as http_status

from apps.common.exceptions import ApiException, ErrorCode

logger = logging.getLogger(__name__)


def to_e164_iraq(phone: str) -> str:
    """Convert canonical 07XXXXXXXXX phone into E.164 +9647XXXXXXXXX."""
    clean = str(phone or "").strip()
    if clean.startswith("+964"):
        return clean
    if clean.startswith("07") and len(clean) == 11:
        return f"+964{clean[1:]}"
    return clean


class WevlixClient:
    """HTTP client for Wevlix generated-OTP send and verify endpoints."""

    def __init__(self, *, transport: httpx.BaseTransport | None = None, timeout: float = 15.0) -> None:
        self.mode = str(getattr(settings, "WEVLIX_MODE", "disabled") or "disabled").strip().lower()
        self.base_url = str(getattr(settings, "WEVLIX_BASE_URL", "https://api.wevlix.com") or "https://api.wevlix.com").rstrip("/")
        self.api_key = str(getattr(settings, "WEVLIX_API_KEY", "") or "").strip()
        self.transport = transport
        self.timeout = timeout

    def is_demo_mode(self) -> bool:
        env = str(getattr(settings, "DJANGO_ENV", "development") or "development").strip().lower()
        allow_demo = bool(getattr(settings, "ALLOW_DEMO_SEED", False))
        return self.mode == "demo" and (env != "production" or allow_demo)

    def is_configured(self) -> bool:
        if self.is_demo_mode():
            return True
        return self.mode in ("sandbox", "live") and bool(self.base_url) and bool(self.api_key)

    def _client(self) -> httpx.Client:
        kwargs: dict[str, Any] = {"timeout": self.timeout}
        if self.transport is not None:
            kwargs["transport"] = self.transport
        return httpx.Client(**kwargs)

    def _endpoint(self, path: str) -> str:
        clean = path.lstrip("/")
        if self.base_url.endswith("/v1"):
            return f"{self.base_url}/{clean}"
        return f"{self.base_url}/v1/{clean}"

    def _headers(self) -> dict[str, str]:
        return {
            "X-API-Key": self.api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def send_otp(self, *, phone: str, request_id: str) -> str:
        """Send a generated 6-digit Arabic OTP via Wevlix and return Wevlix's messageId."""
        if not self.is_configured():
            raise ApiException(
                code=ErrorCode.NOT_FOUND,
                message=("خدمة التحقق غير مفعلة", "OTP service is not enabled"),
                status_code=http_status.HTTP_404_NOT_FOUND,
            )

        body = {
            "to": to_e164_iraq(phone),
            "otpMode": "generated",
            "locale": "ar",
            "clientMessageId": request_id,
        }
        try:
            with self._client() as client:
                resp = client.post(self._endpoint("otp/send"), json=body, headers=self._headers())
        except httpx.HTTPError as exc:
            logger.warning("Wevlix send_otp HTTP error: %s", exc)
            raise ApiException(
                code=ErrorCode.NETWORK,
                message=("تعذر إرسال رمز التحقق حالياً، حاول لاحقاً", "Unable to send verification code right now"),
                status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            ) from exc

        payload: dict[str, Any] = {}
        try:
            raw = resp.json()
            if isinstance(raw, dict):
                payload = raw
        except ValueError:
            payload = {}

        if resp.status_code == http_status.HTTP_429_TOO_MANY_REQUESTS:
            raise ApiException(
                code=ErrorCode.RATE_LIMITED,
                message=("طلبت رموزاً كثيرة: حاول بعد قليل", "Too many OTP requests: try again shortly"),
                status_code=http_status.HTTP_429_TOO_MANY_REQUESTS,
            )
        if resp.status_code >= 400:
            logger.warning("Wevlix send_otp failed (%s): %s", resp.status_code, resp.text[:300])
            raise ApiException(
                code=ErrorCode.NETWORK if resp.status_code >= 500 else ErrorCode.VALIDATION,
                message=("تعذر إرسال رمز التحقق إلى هذا الرقم", "Unable to send verification code to this number"),
                status_code=(
                    http_status.HTTP_503_SERVICE_UNAVAILABLE
                    if resp.status_code >= 500
                    else http_status.HTTP_422_UNPROCESSABLE_ENTITY
                ),
            )

        nested = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        message_id = str(
            payload.get("messageId")
            or payload.get("message_id")
            or nested.get("messageId")
            or nested.get("message_id")
            or ""
        ).strip()
        if not message_id:
            logger.error("Wevlix send_otp response missing messageId: %s", payload)
            raise ApiException(
                code=ErrorCode.NETWORK,
                message=("استجابة غير مكتملة من مزود التحقق", "Incomplete response from verification provider"),
                status_code=http_status.HTTP_502_BAD_GATEWAY,
            )
        return message_id

    def verify_otp(self, *, message_id: str, code: str) -> bool:
        """Verify an OTP code via Wevlix. Returns True if valid, False if rejected as wrong code."""
        if not self.is_configured():
            raise ApiException(
                code=ErrorCode.NOT_FOUND,
                message=("خدمة التحقق غير مفعلة", "OTP service is not enabled"),
                status_code=http_status.HTTP_404_NOT_FOUND,
            )
        if not message_id:
            return False

        body = {
            "messageId": message_id,
            "code": code,
        }
        try:
            with self._client() as client:
                resp = client.post(self._endpoint("otp/verify"), json=body, headers=self._headers())
        except httpx.HTTPError as exc:
            logger.warning("Wevlix verify_otp HTTP error: %s", exc)
            raise ApiException(
                code=ErrorCode.NETWORK,
                message=("تعذر التحقق من الرمز حالياً، حاول مرة أخرى", "Unable to verify code right now"),
                status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            ) from exc

        payload: dict[str, Any] = {}
        try:
            raw = resp.json()
            if isinstance(raw, dict):
                payload = raw
        except ValueError:
            payload = {}

        if resp.status_code == http_status.HTTP_429_TOO_MANY_REQUESTS:
            raise ApiException(
                code=ErrorCode.RATE_LIMITED,
                message=("محاولات كثيرة: اطلب رمزاً جديداً", "Too many tries: ask for a new one"),
                status_code=http_status.HTTP_429_TOO_MANY_REQUESTS,
            )
        if resp.status_code in (http_status.HTTP_400_BAD_REQUEST, http_status.HTTP_422_UNPROCESSABLE_ENTITY):
            return False
        if resp.status_code >= 400:
            logger.warning("Wevlix verify_otp error (%s): %s", resp.status_code, resp.text[:300])
            raise ApiException(
                code=ErrorCode.NETWORK,
                message=("تعذر التحقق من الرمز حالياً، حاول مرة أخرى", "Unable to verify code right now"),
                status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        nested = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        for container in (payload, nested):
            for key in ("verified", "valid", "ok", "matched"):
                if key in container and container[key] is not None:
                    return bool(container[key])
            status_val = str(container.get("status") or "").strip().lower()
            if status_val in ("verified", "valid", "approved", "success", "matched"):
                return True
            if status_val in ("invalid", "failed", "rejected", "wrong", "expired"):
                return False
        if "success" in payload:
            return bool(payload["success"])
        return False
