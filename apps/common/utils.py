"""Core domain helpers: phone normalization, Baghdad calendar, 250-IQD rounding, crypto, images."""
from __future__ import annotations

import base64
import hashlib
import io
import re
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from PIL import Image, ImageOps, UnidentifiedImageError
from rest_framework import status as http_status

from .exceptions import ApiException, ErrorCode

BAGHDAD_TZ = ZoneInfo(getattr(settings, "BUSINESS_TIMEZONE", "Asia/Baghdad"))

_ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
_PHONE_RE = re.compile(r"^07\d{9}$")
_DATA_URL_RE = re.compile(r"^data:image/(jpeg|jpg|png|webp);base64,([A-Za-z0-9+/=\s]+)$", re.IGNORECASE)

# Hard ceiling to prevent DoS bombs on Pillow decode
Image.MAX_IMAGE_PIXELS = 25_000_000


def normalize_phone(raw: str | None, field: str = "phone") -> str:
    """Normalize Iraqi mobile phone formats (+964 / 964 / Arabic digits) into canonical 07XXXXXXXXX."""
    if not raw or not isinstance(raw, str):
        raise ApiException(
            code=ErrorCode.VALIDATION,
            message=("رقم الهاتف مطلوب", "Phone number is required"),
            errors={field: ("رقم الهاتف مطلوب", "Phone number is required")},
        )
    translated = raw.translate(_ARABIC_DIGITS)
    cleaned = re.sub(r"[\s\-().+]", "", translated)
    if cleaned.startswith("00964"):
        cleaned = "0" + cleaned[5:]
    elif cleaned.startswith("964"):
        cleaned = "0" + cleaned[3:]
    elif len(cleaned) == 10 and cleaned.startswith("7"):
        cleaned = "0" + cleaned

    if not _PHONE_RE.match(cleaned):
        msg = ("رقم الهاتف يجب أن يبدأ بـ 07 ويتكون من 11 رقماً", "Phone number must start with 07 and be 11 digits")
        raise ApiException(code=ErrorCode.VALIDATION, message=msg, errors={field: msg})
    return cleaned


def baghdad_now() -> datetime:
    return datetime.now(tz=BAGHDAD_TZ)


def baghdad_today() -> date:
    return baghdad_now().date()


def baghdad_day_of(dt: datetime | date | str) -> str:
    """Return YYYY-MM-DD in Asia/Baghdad calendar matching frontend dayOf()."""
    if isinstance(dt, date) and not isinstance(dt, datetime):
        return dt.isoformat()
    if isinstance(dt, str):
        if len(dt) == 10 and re.match(r"^\d{4}-\d{2}-\d{2}$", dt):
            return dt
        parsed = datetime.fromisoformat(dt.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(BAGHDAD_TZ).date().isoformat()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(BAGHDAD_TZ).date().isoformat()


def round_by(amount: int | Decimal, step: int = 250) -> int:
    """Round non-negative IQD amount to nearest `step` IQD (0 = nearest integer, half-up)."""
    dec = Decimal(amount)
    if dec <= 0:
        return 0
    step_int = int(step)
    if step_int <= 0:
        return int(dec.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    step_dec = Decimal(step_int)
    units = (dec / step_dec).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return int(units * step_dec)


def round_250(amount: int | Decimal) -> int:
    """Round non-negative IQD amount to nearest 250 IQD (half-up, matching commercial rules)."""
    return round_by(amount, 250)


def calc_percent_round(amount: int | Decimal, percent: Decimal | int | str) -> int:
    """Exact Decimal percentage rounded half-up to nearest whole IQD."""
    dec_amount = Decimal(amount)
    dec_pct = Decimal(str(percent))
    val = (dec_amount * dec_pct / Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return int(val)


def _get_fernet() -> Fernet:
    configured = getattr(settings, "FIELD_ENCRYPTION_KEY", "")
    if configured:
        key_bytes = configured.encode("utf-8") if isinstance(configured, str) else configured
        return Fernet(key_bytes)
    if not settings.DEBUG and getattr(settings, "DJANGO_ENV", "development") == "production":
        raise RuntimeError("FIELD_ENCRYPTION_KEY must be explicitly configured in production.")
    digest = hashlib.sha256(settings.SECRET_KEY.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_secret(plaintext: str) -> str:
    return _get_fernet().encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt_secret(token: str) -> str:
    try:
        return _get_fernet().decrypt(token.encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:
        raise ValueError("Unable to decrypt stored secret token.") from exc


def decode_image_data_url(
    data_url: str,
    *,
    max_bytes: int = 5 * 1024 * 1024,
    max_dim: int = 1600,
) -> tuple[bytes, str, str]:
    """Validate base64 image data URL with Pillow, strip metadata, return (bytes, ext, mime_type)."""
    if not isinstance(data_url, str):
        raise ApiException(
            code=ErrorCode.VALIDATION,
            message=("صيغة الصورة غير صالحة", "Invalid image format"),
        )
    match = _DATA_URL_RE.match(data_url.strip())
    if not match:
        raise ApiException(
            code=ErrorCode.VALIDATION,
            message=("يجب إرسال الصورة بصيغة JPEG أو PNG أو WebP صالحة", "Image must be a valid JPEG, PNG, or WebP data URL"),
        )

    b64_payload = re.sub(r"\s+", "", match.group(2))
    # Approximate decoded size check prior to allocating large buffer
    if len(b64_payload) > int(max_bytes * 1.4):
        raise ApiException(
            code=ErrorCode.VALIDATION,
            message=("حجم الصورة يتجاوز الحد المسموح (5 ميجابايت)", "Image size exceeds maximum allowed limit (5 MB)"),
        )

    try:
        raw = base64.b64decode(b64_payload, validate=True)
    except Exception as exc:
        raise ApiException(
            code=ErrorCode.VALIDATION,
            message=("بيانات الصورة غير صالحة", "Corrupted base64 image payload"),
        ) from exc

    if len(raw) == 0 or len(raw) > max_bytes:
        raise ApiException(
            code=ErrorCode.VALIDATION,
            message=("حجم الصورة غير مسموح", "Image payload size out of bounds"),
        )

    try:
        img_verify = Image.open(io.BytesIO(raw))
        img_verify.verify()
        img = Image.open(io.BytesIO(raw))
        fmt = (img.format or "").upper()
        if fmt not in ("JPEG", "PNG", "WEBP"):
            raise ValueError("Unsupported image format")
        img = ImageOps.exif_transpose(img)
        if max(img.width, img.height) > max_dim:
            img.thumbnail((max_dim, max_dim), Image.Resampling.LANCZOS)

        out = io.BytesIO()
        if fmt == "PNG":
            img.save(out, format="PNG", optimize=True)
            return out.getvalue(), "png", "image/png"
        if fmt == "WEBP":
            img.save(out, format="WEBP", quality=85)
            return out.getvalue(), "webp", "image/webp"

        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        img.save(out, format="JPEG", quality=82, optimize=True)
        return out.getvalue(), "jpg", "image/jpeg"
    except (UnidentifiedImageError, ValueError, OSError) as exc:
        raise ApiException(
            code=ErrorCode.VALIDATION,
            message=("الملف المرسل ليس صورة صالحة", "Uploaded file is not a valid image"),
            status_code=http_status.HTTP_400_BAD_REQUEST,
        ) from exc


def client_ip(request) -> str | None:
    """Extract client IP address from X-Forwarded-For or REMOTE_ADDR."""
    if request is None:
        return None
    xff = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if xff:
        return xff.split(",")[0].strip() or None
    return request.META.get("REMOTE_ADDR") or None
