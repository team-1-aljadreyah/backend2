"""Authenticated Qi Card online payment gateway client and webhook verifier."""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx
from django.conf import settings
from rest_framework import status as http_status

from apps.common.exceptions import ApiException, ErrorCode

logger = logging.getLogger(__name__)


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
        self.username = str(getattr(settings, "QI_CARD_USERNAME", "") or "").strip()
        self.password = str(getattr(settings, "QI_CARD_PASSWORD", "") or "").strip()
        self.api_key = str(getattr(settings, "QI_CARD_API_KEY", "") or "").strip()
        if not self.username and ":" in self.api_key:
            u, p = self.api_key.split(":", 1)
            self.username = u.strip()
            self.password = p.strip()
        self.webhook_secret = str(getattr(settings, "QI_CARD_WEBHOOK_SECRET", "") or "").strip()
        self.public_key = str(getattr(settings, "QI_CARD_PUBLIC_KEY", "") or "").strip()
        self.transport = transport
        self.timeout = timeout

    def is_configured(self) -> bool:
        has_auth = bool(self.username and self.password) or bool(self.api_key)
        has_terminal = bool(self.terminal_id) or bool(self.merchant_id)
        return self.mode in ("sandbox", "live") and bool(self.base_url) and has_auth and has_terminal

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

    def _endpoint(self, path: str) -> str:
        clean = path.lstrip("/")
        if self.base_url.endswith("/api/v1"):
            return f"{self.base_url}/{clean}"
        return f"{self.base_url}/api/v1/{clean}"

    def _headers(self) -> dict[str, str]:
        if self.username and self.password:
            creds = base64.b64encode(f"{self.username}:{self.password}".encode("utf-8")).decode("ascii")
            auth_header = f"Basic {creds}"
        else:
            auth_header = f"Bearer {self.api_key}"
        headers = {
            "Authorization": auth_header,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self.terminal_id:
            headers["X-Terminal-Id"] = self.terminal_id
        if self.merchant_id:
            headers["X-Merchant-Id"] = self.merchant_id
        return headers

    @staticmethod
    def map_status(raw_status: str | None) -> str:
        s = (raw_status or "").strip().upper()
        if s in ("SUCCESS", "SUCCEEDED", "PAID", "CAPTURED", "AUTHENTICATION_SUCCESS"):
            return "success"
        if s == "AUTHENTICATION_FAILED":
            return "authentication_failed"
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
        payload: dict[str, Any] = {
            "requestId": request_id,
            "amount": int(amount),
            "currency": "IQD",
            "locale": "ar_IQ",
            "finishPaymentUrl": finish_url,
            "notificationUrl": notification_url,
        }
        if customer_name or customer_phone:
            cinfo: dict[str, str] = {}
            if customer_name:
                cinfo["firstName"] = customer_name[:80]
            if customer_phone:
                cinfo["phone"] = customer_phone[:30]
            payload["customerInfo"] = cinfo
        try:
            with self._client() as client:
                resp = client.post(
                    self._endpoint("payment"),
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
            logger.warning("Qi Card create_payment error %s: %s", resp.status_code, resp.text[:400])
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
                    self._endpoint(f"payment/{payment_id}/status"),
                    headers=self._headers(),
                )
        except httpx.HTTPError as exc:
            raise ApiException(
                code=ErrorCode.NETWORK,
                message=("تعذر الاستعلام عن حالة الدفع لدى Qi Card", "Unable to query Qi Card payment status"),
                status_code=http_status.HTTP_502_BAD_GATEWAY,
            ) from exc

        if resp.status_code >= 400:
            logger.warning("Qi Card get_payment_status error %s: %s", resp.status_code, resp.text[:400])
            raise ApiException(
                code=ErrorCode.BAD_STATE,
                message=("تعذر التحقق من حالة الدفع لدى Qi Card", "Qi Card payment verification failed"),
                status_code=http_status.HTTP_502_BAD_GATEWAY,
            )

        body = resp.json() if resp.content else {}
        details = body.get("details") if isinstance(body.get("details"), dict) else {}
        pm = body.get("paymentMethod") if isinstance(body.get("paymentMethod"), dict) else {}
        masked = str(
            body.get("maskedPan")
            or details.get("maskedPan")
            or pm.get("maskedPan")
            or body.get("card")
            or ""
        ).strip()
        mapped = self.map_status(body.get("status"))
        if mapped == "created" and body.get("canceled") is True:
            mapped = "cancelled"
        return QiStatusResult(
            payment_id=str(body.get("paymentId") or payment_id),
            status=mapped,
            masked_card=masked,
            raw=body,
        )

    @staticmethod
    def _verify_rsa_signature(*, payload: dict[str, Any], signature_b64: str, public_key_pem: str) -> bool:
        try:
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import padding

            raw_amt = payload.get("amount")
            if raw_amt is None or raw_amt == "":
                amt_str = "-"
            else:
                s_amt = str(raw_amt)
                amt_str = f"{s_amt}.000" if "." not in s_amt else s_amt
            data_str = "|".join([
                str(payload.get("paymentId") or "-"),
                amt_str,
                str(payload.get("currency") or "-"),
                str(payload.get("creationDate") or "-"),
                str(payload.get("status") or "-"),
            ])
            pub = serialization.load_pem_public_key(public_key_pem.encode("utf-8"))
            sig_bytes = base64.b64decode(signature_b64)
            pub.verify(sig_bytes, data_str.encode("utf-8"), padding.PKCS1v15(), hashes.SHA256())  # type: ignore[union-attr]
            return True
        except Exception:
            return False

    def verify_webhook_signature(
        self, *, raw_body: bytes, headers: Mapping[str, str], payload: dict[str, Any] | None = None
    ) -> bool:
        sig_header = ""
        for key in ("HTTP_X_QI_SIGNATURE", "X-Qi-Signature", "HTTP_X_SIGNATURE", "X-Signature", "HTTP_SIGNATURE", "Signature"):
            val = headers.get(key)
            if val:
                sig_header = str(val).strip()
                break

        if not self.webhook_secret and not self.public_key:
            if self.mode == "sandbox":
                return False
            raise ApiException(
                code=ErrorCode.FORBIDDEN,
                message=("توقيع البوابة غير مهيأ", "Webhook verification secret is not configured"),
                status_code=http_status.HTTP_403_FORBIDDEN,
            )

        if not sig_header:
            raise ApiException(
                code=ErrorCode.FORBIDDEN,
                message=("توقيع البوابة مفقود", "Missing gateway signature"),
                status_code=http_status.HTTP_403_FORBIDDEN,
            )

        if self.webhook_secret:
            provided = sig_header[7:] if sig_header.lower().startswith("sha256=") else sig_header
            expected = hmac.new(self.webhook_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
            if hmac.compare_digest(expected.lower(), provided.lower()):
                return True

        if self.public_key and payload is not None:
            if self._verify_rsa_signature(payload=payload, signature_b64=sig_header, public_key_pem=self.public_key):
                return True

        raise ApiException(
            code=ErrorCode.FORBIDDEN,
            message=("توقيع بوابة الدفع غير صحيح", "Invalid gateway webhook signature"),
            status_code=http_status.HTTP_403_FORBIDDEN,
        )
