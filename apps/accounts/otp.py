"""Wevlix-backed OTP challenge lifecycle (send, verify, single-use token verification)."""
from __future__ import annotations

import math
import re
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from django.contrib.auth.hashers import check_password, make_password
from django.db import transaction
from rest_framework import status as http_status

from apps.accounts.business_rules import rules_of
from apps.accounts.models import OtpChallenge, OtpPurpose, User
from apps.common.exceptions import ApiException, ErrorCode
from apps.common.utils import normalize_phone
from apps.customers.models import Customer
from integrations.wevlix import WevlixClient

OTP_TOKEN_TTL_SECONDS = 15 * 60

_ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")


def _otp_limits() -> tuple[int, int, int, int]:
    """Return (code_ttl_seconds, resend_seconds, per_hour, max_tries) from active rules."""
    o = rules_of().get("otp") or {}
    return (
        max(60, int(o.get("code_minutes", 5)) * 60),
        max(15, int(o.get("resend_seconds", 60))),
        max(1, int(o.get("per_hour", 5))),
        max(1, int(o.get("tries", 5))),
    )


def _get_gateway(gateway: WevlixClient | None = None) -> WevlixClient:
    return gateway if gateway is not None else WevlixClient()


def send_otp_challenge(
    *,
    raw_phone: Any,
    purpose: Any,
    actor: User | None = None,
    gateway: WevlixClient | None = None,
) -> dict[str, str]:
    """Send a 6-digit OTP via Wevlix (no code ever returned to caller)."""
    gw = _get_gateway(gateway)
    if not gw.is_configured():
        raise ApiException(
            code=ErrorCode.NOT_FOUND,
            message=("خدمة التحقق غير مفعلة", "OTP service is not enabled"),
            status_code=http_status.HTTP_404_NOT_FOUND,
        )

    purpose_val = str(purpose or "").strip().lower()
    if purpose_val not in OtpPurpose.values:
        raise ApiException(
            code=ErrorCode.VALIDATION,
            message=("غرض غير معروف", "Unknown purpose"),
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            errors={"purpose": ("غرض غير معروف", "Unknown purpose")},
        )

    try:
        phone = normalize_phone(raw_phone)
    except ApiException as exc:
        raise ApiException(
            code=exc.code,
            message=exc.message,
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            errors=exc.errors,
        ) from exc

    taken_user = User.objects.filter(phone=phone, deleted=False).exists()
    taken_customer = Customer.objects.filter(phone=phone).exists()

    if purpose_val == OtpPurpose.REGISTER and (taken_user or taken_customer):
        msg = ("الرقم مسجّل من قبل", "Already registered")
        raise ApiException(
            code=ErrorCode.BAD_STATE,
            message=("لهذا الرقم حساب من قبل: سجّل الدخول به", "This number already has an account: sign in"),
            status_code=http_status.HTTP_409_CONFLICT,
            errors={"phone": msg},
        )

    auth_user = actor if (actor is not None and getattr(actor, "is_authenticated", False)) else None
    if purpose_val == OtpPurpose.PHONE:
        if auth_user is None:
            raise ApiException(
                code=ErrorCode.UNAUTHENTICATED,
                message=("سجّل الدخول أولاً", "Sign in first"),
                status_code=http_status.HTTP_401_UNAUTHORIZED,
            )
        if auth_user.phone == phone:
            raise ApiException(
                code=ErrorCode.VALIDATION,
                message=("هذا رقمك الحالي", "That's your current number"),
                status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
                errors={"phone": ("اكتب الرقم الجديد", "Write the new number")},
            )
        if taken_user:
            raise ApiException(
                code=ErrorCode.BAD_STATE,
                message=("هذا الرقم لحساب آخر", "That number belongs to another account"),
                status_code=http_status.HTTP_409_CONFLICT,
                errors={"phone": ("الرقم مستخدم", "In use")},
            )

    code_ttl_seconds, resend_seconds, max_per_hour, _ = _otp_limits()
    now = datetime.now(tz=UTC)
    hour_ago = now - timedelta(hours=1)

    recent_for_phone = list(
        OtpChallenge.objects.filter(phone=phone, created_at__gte=hour_ago).order_by("created_at", "id")
    )
    same_purpose = [c for c in recent_for_phone if c.purpose == purpose_val]
    if same_purpose:
        last = same_purpose[-1]
        elapsed = (now - last.created_at).total_seconds()
        if elapsed < resend_seconds:
            wait = max(1, math.ceil(resend_seconds - elapsed))
            raise ApiException(
                code=ErrorCode.RATE_LIMITED,
                message=(f"انتظر {wait} ثانية ثم أعد الإرسال", f"Wait {wait} s and send again"),
                status_code=http_status.HTTP_429_TOO_MANY_REQUESTS,
            )
    if len(recent_for_phone) >= max_per_hour:
        raise ApiException(
            code=ErrorCode.RATE_LIMITED,
            message=("طلبت رموزاً كثيرة: حاول بعد ساعة", "Too many codes: try in an hour"),
            status_code=http_status.HTTP_429_TOO_MANY_REQUESTS,
        )

    request_id = secrets.token_hex(12)
    real_send = purpose_val != OtpPurpose.RESET or taken_user
    message_id = ""
    code_hash = ""
    sandbox_code: str | None = None
    if real_send:
        if gw.is_demo_mode():
            sandbox_code = str(100000 + secrets.randbelow(900000))
            code_hash = make_password(sandbox_code)
        else:
            message_id = gw.send_otp(phone=phone, request_id=request_id)

    save_now = datetime.now(tz=UTC)
    expires_at = save_now + timedelta(seconds=code_ttl_seconds)
    resend_at = save_now + timedelta(seconds=resend_seconds)
    with transaction.atomic():
        OtpChallenge.objects.create(
            request_id=request_id,
            phone=phone,
            purpose=purpose_val,
            user=auth_user if purpose_val == OtpPurpose.PHONE else None,
            wevlix_message_id=message_id,
            code_hash=code_hash,
            real_sent=real_send,
            expires_at=expires_at,
            resend_at=resend_at,
        )

    out: dict[str, str] = {
        "request_id": request_id,
        "expires_at": expires_at.isoformat(),
        "resend_at": resend_at.isoformat(),
    }
    if sandbox_code is not None:
        out["sandbox_code"] = sandbox_code
    return out


def _persist_failed_otp_attempt(challenge_id: int, max_tries: int) -> int:
    with transaction.atomic():
        ch = OtpChallenge.objects.select_for_update().get(id=challenge_id)
        if ch.attempts < max_tries:
            ch.attempts += 1
            ch.save(update_fields=["attempts"])
        return ch.attempts


def verify_otp_challenge(
    *,
    request_id: Any,
    raw_code: Any,
    gateway: WevlixClient | None = None,
) -> dict[str, str]:
    """Verify user-submitted 6-digit OTP against Wevlix and mint a single-use 15-minute otp_token."""
    gw = _get_gateway(gateway)
    if not gw.is_configured():
        raise ApiException(
            code=ErrorCode.NOT_FOUND,
            message=("خدمة التحقق غير مفعلة", "OTP service is not enabled"),
            status_code=http_status.HTTP_404_NOT_FOUND,
        )

    req_id = str(request_id or "").strip()
    challenge = OtpChallenge.objects.filter(request_id=req_id).first()
    if challenge is None:
        raise ApiException(
            code=ErrorCode.NOT_FOUND,
            message=("انتهت صلاحية الرمز: اطلب رمزاً جديداً", "The code expired: ask for a new one"),
            status_code=http_status.HTTP_404_NOT_FOUND,
        )

    if challenge.otp_token:
        raise ApiException(
            code=ErrorCode.BAD_STATE,
            message=("استُخدم هذا الرمز من قبل", "This code was already used"),
            status_code=http_status.HTTP_409_CONFLICT,
        )

    now = datetime.now(tz=UTC)
    if now > challenge.expires_at:
        raise ApiException(
            code=ErrorCode.BAD_STATE,
            message=("انتهت صلاحية الرمز: اطلب رمزاً جديداً", "The code expired: ask for a new one"),
            status_code=http_status.HTTP_410_GONE,
        )

    _, _, _, max_tries = _otp_limits()
    if challenge.attempts >= max_tries:
        raise ApiException(
            code=ErrorCode.RATE_LIMITED,
            message=("محاولات كثيرة: اطلب رمزاً جديداً", "Too many tries: ask for a new one"),
            status_code=http_status.HTTP_429_TOO_MANY_REQUESTS,
            errors={"code": ("انتهت المحاولات: اطلب رمزاً جديداً", "No tries left: ask for a new one")},
        )

    clean_code = str(raw_code or "").translate(_ARABIC_DIGITS).strip()
    is_valid_format = bool(re.match(r"^\d{6}$", clean_code))
    verified = False
    if is_valid_format and challenge.real_sent:
        if challenge.code_hash:
            verified = check_password(clean_code, challenge.code_hash)
        elif challenge.wevlix_message_id:
            verified = gw.verify_otp(message_id=challenge.wevlix_message_id, code=clean_code)

    if not verified:
        attempts_now = _persist_failed_otp_attempt(challenge.id, max_tries)
        left = max_tries - attempts_now
        if left <= 0:
            raise ApiException(
                code=ErrorCode.RATE_LIMITED,
                message=("محاولات كثيرة: اطلب رمزاً جديداً", "Too many tries: ask for a new one"),
                status_code=http_status.HTTP_429_TOO_MANY_REQUESTS,
                errors={"code": ("انتهت المحاولات: اطلب رمزاً جديداً", "No tries left: ask for a new one")},
            )
        raise ApiException(
            code=ErrorCode.VALIDATION,
            message=("الرمز غير صحيح", "Wrong code"),
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            errors={"code": (f"الرمز غير صحيح، باقي {left} محاولات", f"Wrong code, {left} tries left")},
        )

    token = secrets.token_urlsafe(24)
    token_expires = now + timedelta(seconds=OTP_TOKEN_TTL_SECONDS)
    with transaction.atomic():
        ch = OtpChallenge.objects.select_for_update().get(id=challenge.id)
        if ch.otp_token:
            raise ApiException(
                code=ErrorCode.BAD_STATE,
                message=("استُخدم هذا الرمز من قبل", "This code was already used"),
                status_code=http_status.HTTP_409_CONFLICT,
            )
        if datetime.now(tz=UTC) > ch.expires_at:
            raise ApiException(
                code=ErrorCode.BAD_STATE,
                message=("انتهت صلاحية الرمز: اطلب رمزاً جديداً", "The code expired: ask for a new one"),
                status_code=http_status.HTTP_410_GONE,
            )
        ch.otp_token = token
        ch.token_expires_at = token_expires
        ch.save(update_fields=["otp_token", "token_expires_at"])

    return {
        "otp_token": token,
        "expires_at": token_expires.isoformat(),
    }


def spend_otp_token(*, otp_token: Any, phone: str, purpose: str) -> OtpChallenge:
    """Validate and atomically consume a single-use otp_token bound to (phone, purpose)."""
    token = str(otp_token or "").strip()
    now = datetime.now(tz=UTC)
    ch = OtpChallenge.objects.select_for_update().filter(otp_token=token).first() if token else None
    if (
        ch is None
        or ch.used_at is not None
        or ch.phone != phone
        or ch.purpose != purpose
        or ch.token_expires_at is None
        or ch.token_expires_at <= now
    ):
        raise ApiException(
            code=ErrorCode.VALIDATION,
            message=("تحقّق من رقم هاتفك أولاً", "Verify your phone number first"),
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            errors={"otp_token": ("أدخل رمز التحقق الذي وصلك", "Enter the code you received")},
        )
    ch.used_at = now
    ch.save(update_fields=["used_at"])
    return ch
