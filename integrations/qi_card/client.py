"""Authenticated Qi Card online payment gateway client and webhook verifier."""
from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx
from django.conf import settings
from rest_framework import status as http_status

from apps.common.exceptions import ApiException, ErrorCode


@dataclass(frozen=True)
class QiCreateResult:
    payment_id: str
    form_url: str
    status: str
    raw: dict[str, Any]


@dataclass(frozen=True)
class QiStatusResult:
    payment_id: str
    status: str
    masked_card: str
    raw: dict[str, Any]


class QiCardGateway:
    """
    Production Qi Card REST client + cryptographic webhook verifier.
    Refuses to operate when QI_CARD_MODE is 'disabled' or credentials are missing
    (no fake payment simulation in production/backend).
    """

    def __init__(self, *, transport: httpx.BaseTransport | None = None, timeout: float = 15.0) -> None:
        self.mode = str(getattr(settings, "QI_CARD_MODE", "disabled") or "disabled").strip().lower()
        self.base_url = str(getattr(settings, "QI_CARD_BASE_URL", "") or "").rstrip("/")
        self.merchant_id = str(getattr(settings, "QI_CARD_MERCHANT_ID", "") or "").strip()
        self.terminal_id = str(getattr(settings, "QI_CARD_TERMINAL_ID", "") or "").strip()
        self.api_key = str(getattr(settings, "QI_CARD_API_KEY", "") or "").strip()
        self.webhook_secret = str(getattr(settings, "QI_CARD_WEBHOOK_SECRET", "") or "").strip()
        self.transport = transport
        self.timeout = timeout

    def is_configured(self) -> bool:
        return (
            self.mode in ("sandbox", "live")
            and bool(self.base_url)
            and bool(self.merchant_id)
            and bool(self.api_key)
            and bool(self.webhook_secret)
        )

    def _require_configured(self) -> None:
        if not self.is_configured():
            raise ApiException(
                code=ErrorCode.BAD_STATE,
                message=("بوابة الدفع الإلكتروني غير مهيأة حالياً", "Online payment gateway is not configured"),
                status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            )

    def _client(self) -> httpx.Client:
        kwargs: dict[str, Any] = {"timeout": self.timeout}
        if self.transport is not None:
            kwargs["transport"] = self.transport
        return httpx.Client(**kwargs)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "X-Merchant-Id": self.merchant_id,
            "X-Terminal-Id": self.terminal_id,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    @staticmethod
    def map_status(raw_status: str | None) -> str:
        s = (raw_status or "").strip().upper()
        if s in ("SUCCESS", "SUCCEEDED", "PAID", "CAPTURED", "AUTHENTICATION_SUCCESS"):
            return "success"
        if s in ("FAILED", "DECLINED", "ERROR"):
            return "failed"
        if s in ("CANCELLED", "CANCELED"):
            return "cancelled"
        if s == "EXPIRED":
            return "expired"
        return "created"

    def create_payment(
        self,
        *,
        request_id: str,
        amount: int,
        finish_url: str,
        notification_url: str,
        customer_name: str = "",
        customer_phone: str = "",
    ) -> QiCreateResult:
        self._require_configured()
        payload = {
            "requestId": request_id,
            "amount": int(amount),
            "currency": "IQD",
            "finishPaymentUrl": finish_url,
            "notificationUrl": notification_url,
            "customerInfo": {
                "name": customer_name,
                "phone": customer_phone,
            },
        }
        try:
            with self._client() as client:
                resp = client.post(
                    f"{self.base_url}/api/v1/payment",
                    json=payload,
                    headers=self._headers(),
                )
        except httpx.HTTPError as exc:
            raise ApiException(
                code=ErrorCode.NETWORK,
                message=("تعذر الاتصال ببوابة Qi Card", "Unable to reach Qi Card gateway"),
                status_code=http_status.HTTP_502_BAD_GATEWAY,
            ) from exc

        if resp.status_code >= 400:
            raise ApiException(
                code=ErrorCode.BAD_STATE,
                message=("رفضت بوابة Qi Card إنشاء معاملة الدفع", "Qi Card gateway rejected payment creation"),
                status_code=http_status.HTTP_502_BAD_GATEWAY,
            )

        body = resp.json() if resp.content else {}
        pid = str(body.get("paymentId") or body.get("id") or "").strip()
        form_url = str(body.get("formUrl") or body.get("redirectUrl") or "").strip()
        if not pid or not form_url:
            raise ApiException(
                code=ErrorCode.BAD_STATE,
                message=("استجابة غير مكتملة من بوابة Qi Card", "Incomplete response from Qi Card gateway"),
                status_code=http_status.HTTP_502_BAD_GATEWAY,
            )

        return QiCreateResult(
            payment_id=pid,
            form_url=form_url,
            status=self.map_status(body.get("status")),
            raw=body,
        )

    def get_payment_status(self, payment_id: str) -> QiStatusResult:
        self._require_configured()
        try:
            with self._client() as client:
                resp = client.get(
                    f"{self.base_url}/api/v1/payment/{payment_id}",
                    headers=self._headers(),
                )
        except httpx.HTTPError as exc:
            raise ApiException(
                code=ErrorCode.NETWORK,
                message=("تعذر الاستعلام عن حالة الدفع لدى Qi Card", "Unable to query Qi Card payment status"),
                status_code=http_status.HTTP_502_BAD_GATEWAY,
            ) from exc

        if resp.status_code >= 400:
            raise ApiException(
                code=ErrorCode.BAD_STATE,
                message=("تعذر التحقق من حالة الدفع لدى Qi Card", "Qi Card payment verification failed"),
                status_code=http_status.HTTP_502_BAD_GATEWAY,
            )

        body = resp.json() if resp.content else {}
        masked = str(
            body.get("maskedPan")
            or (body.get("paymentMethod") or {}).get("maskedPan")
            or body.get("card")
            or ""
        ).strip()
        return QiStatusResult(
            payment_id=str(body.get("paymentId") or payment_id),
            status=self.map_status(body.get("status")),
            masked_card=masked,
            raw=body,
        )

    def verify_webhook_signature(self, *, raw_body: bytes, headers: Mapping[str, str]) -> None:
        if not self.webhook_secret:
            raise ApiException(
                code=ErrorCode.FORBIDDEN,
                message=("توقيع البوابة غير مهيأ", "Webhook verification secret is not configured"),
                status_code=http_status.HTTP_403_FORBIDDEN,
            )

        sig_header = ""
        for key in ("HTTP_X_QI_SIGNATURE", "X-Qi-Signature", "HTTP_X_SIGNATURE", "X-Signature", "HTTP_SIGNATURE", "Signature"):
            val = headers.get(key)
            if val:
                sig_header = str(val).strip()
                break

        if not sig_header:
            raise ApiException(
                code=ErrorCode.FORBIDDEN,
                message=("توقيع البوابة مفقود", "Missing gateway signature"),
                status_code=http_status.HTTP_403_FORBIDDEN,
            )

        provided = sig_header[7:] if sig_header.lower().startswith("sha256=") else sig_header
        expected = hmac.new(self.webhook_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected.lower(), provided.lower()):
            raise ApiException(
                code=ErrorCode.FORBIDDEN,
                message=("توقيع بوابة الدفع غير صحيح", "Invalid gateway webhook signature"),
                status_code=http_status.HTTP_403_FORBIDDEN,
            )
