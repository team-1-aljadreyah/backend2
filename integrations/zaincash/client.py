"""ZainCash Payment Gateway API v2 client integration.

Integrates with ZainCash UAT (https://pg-api-uat.zaincash.iq) and production:
1. POST /oauth2/token (client credentials -> bearer token, cached ~24h)
2. POST /api/v2/payment-gateway/transaction/init (orderId, amount IQD, returnUrls -> redirectUrl, transactionId)
3. Return token verification (JWT HS256 signed with client_secret)
4. GET /api/v2/payment-gateway/transaction/inquiry/{transactionId} (inquire status from gateway)
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Defaults point to public UAT sandbox test keys
DEFAULT_UAT_BASE = "https://pg-api-uat.zaincash.iq"
DEFAULT_UAT_CLIENT_ID = "758055f4a8044779a35f6ceb69f858b3"
DEFAULT_UAT_CLIENT_SECRET = "bibLCGTxVAig5To3OLLKPJQMlRR7Pefp"


def _b64url_decode(s: str) -> bytes:
    s = s.replace("-", "+").replace("_", "/")
    padding = len(s) % 4
    if padding:
        s += "=" * (4 - padding)
    return base64.b64decode(s)


class ZainCashClient:
    def __init__(
        self,
        base_url: str | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
        mode: str | None = None,
    ):
        self.base_url = (
            base_url
            or os.getenv("ZAINCASH_BASE_URL")
            or DEFAULT_UAT_BASE
        ).rstrip("/")
        self.client_id = (
            client_id
            or os.getenv("ZAINCASH_CLIENT_ID")
            or DEFAULT_UAT_CLIENT_ID
        )
        self.client_secret = (
            client_secret
            or os.getenv("ZAINCASH_CLIENT_SECRET")
            or DEFAULT_UAT_CLIENT_SECRET
        )
        self.mode = (mode or os.getenv("ZAINCASH_MODE") or "sandbox").lower()
        self._cached_token: str | None = None
        self._token_expires_at: float = 0.0

    def is_enabled(self) -> bool:
        return self.mode != "disabled"

    def get_access_token(self) -> str:
        """Fetch and cache OAuth2 Bearer token (~24 hours lifetime)."""
        now = time.time()
        if self._cached_token and self._token_expires_at > (now + 60):
            return self._cached_token

        url = f"{self.base_url}/oauth2/token"
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        data = {
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "scope": "payment:read payment:write",
        }
        with httpx.Client(timeout=10.0) as client:
            resp = client.post(url, headers=headers, data=data)
            if resp.status_code >= 400:
                logger.error(f"ZainCash token error {resp.status_code}: {resp.text}")
                raise RuntimeError(f"ZainCash OAuth2 failure: HTTP {resp.status_code}")
            d = resp.json()

        token = d.get("access_token")
        expires_in = int(d.get("expires_in") or 86400)
        if not token:
            raise RuntimeError("ZainCash OAuth2 response missing access_token")

        self._cached_token = token
        self._token_expires_at = now + expires_in
        return token

    def init_transaction(
        self,
        *,
        amount: int,
        order_id: str,
        reference: str,
        return_url: str,
        lang: str = "ar",
        service: str = "Al-Nahrain Wholesale Goods",
    ) -> dict[str, str]:
        """
        Initialize payment gateway transaction on ZainCash.
        Returns: {"transaction_id": "...", "redirect_url": "..."}
        """
        token = self.get_access_token()
        url = f"{self.base_url}/api/v2/payment-gateway/transaction/init"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        payload = {
            "language": "ar" if lang.startswith("ar") else "en",
            "externalReferenceId": reference,
            "orderId": order_id,
            "amount": {"value": str(amount), "currency": "IQD"},
            "serviceType": service,
            "redirectUrls": {"successUrl": return_url, "failureUrl": return_url},
        }

        with httpx.Client(timeout=10.0) as client:
            resp = client.post(url, headers=headers, json=payload)
            if resp.status_code >= 400:
                logger.error(f"ZainCash init error {resp.status_code}: {resp.text}")
                raise RuntimeError(f"ZainCash init failed: HTTP {resp.status_code} - {resp.text}")
            d = resp.json()

        transaction_details = d.get("transactionDetails") or {}
        tx_id = transaction_details.get("transactionId") or d.get("transactionId")
        redirect_url = d.get("redirectUrl")

        if not tx_id or not redirect_url:
            raise RuntimeError(f"ZainCash init returned incomplete response: {d}")

        return {
            "transaction_id": str(tx_id),
            "redirect_url": str(redirect_url),
        }

    def verify_return_token(self, jwt_token: str) -> dict[str, Any] | None:
        """
        Verify HMAC-SHA256 signature on ZainCash return JWT and decode payload.
        """
        try:
            parts = jwt_token.strip().split(".")
            if len(parts) != 3:
                return None
            h_b64, p_b64, s_b64 = parts
            signing_input = f"{h_b64}.{p_b64}".encode("utf-8")
            expected_sig = hmac.new(
                self.client_secret.encode("utf-8"),
                signing_input,
                hashlib.sha256,
            ).digest()
            actual_sig = _b64url_decode(s_b64)
            if not hmac.compare_digest(expected_sig, actual_sig):
                logger.warning("ZainCash return JWT signature mismatch")
                return None

            payload_bytes = _b64url_decode(p_b64)
            payload = json.loads(payload_bytes.decode("utf-8"))
            data = payload.get("data") or {}
            return {
                "status": data.get("currentStatus") or data.get("status"),
                "transaction_id": data.get("transactionId"),
                "order_id": data.get("orderId"),
                "raw": payload,
            }
        except Exception as exc:
            logger.warning(f"Failed to decode ZainCash return token: {exc}")
            return None

    def inquire_transaction(self, transaction_id: str) -> dict[str, Any]:
        """
        Query ZainCash server for authoritative status.
        Returns: {"status": "SUCCESS" | "FAILED" | ..., "phone": str | None}
        """
        token = self.get_access_token()
        url = f"{self.base_url}/api/v2/payment-gateway/transaction/inquiry/{transaction_id}"
        headers = {"Authorization": f"Bearer {token}"}

        with httpx.Client(timeout=10.0) as client:
            resp = client.get(url, headers=headers)
            if resp.status_code >= 400:
                logger.error(f"ZainCash inquiry error {resp.status_code}: {resp.text}")
                raise RuntimeError(f"ZainCash inquiry failed: HTTP {resp.status_code}")
            d = resp.json()

        cust = d.get("customer") or {}
        phone = cust.get("phone")
        return {
            "status": d.get("status") or "PENDING",
            "phone": str(phone) if phone else None,
            "raw": d,
        }

    @staticmethod
    def map_status(gateway_status: str) -> str:
        """Map ZainCash status to our internal OnlineStatus."""
        u = str(gateway_status or "").strip().upper()
        if u in ("SUCCESS", "COMPLETED"):
            return "success"
        if u in ("FAILED", "FAILURE", "REJECTED", "DECLINED"):
            return "failed"
        if u in ("CANCELLED", "CANCELED", "EXPIRED"):
            return "canceled"
        return "created"


_client_instance: ZainCashClient | None = None


def get_zaincash_client() -> ZainCashClient:
    global _client_instance
    if _client_instance is None:
        _client_instance = ZainCashClient()
    return _client_instance
