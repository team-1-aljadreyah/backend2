"""Phase 1 foundation unit and HTTP integration tests."""
from __future__ import annotations

import base64
import io

import pytest
from django.db import connection
from PIL import Image
from rest_framework.test import APIClient

from apps.common.exceptions import ApiException, ErrorCode
from apps.common.utils import (
    baghdad_day_of,
    calc_percent_round,
    decode_image_data_url,
    decrypt_secret,
    encrypt_secret,
    normalize_phone,
    round_250,
)
from integrations.storage.local import LocalMediaStorage


def test_phone_normalization_variants():
    assert normalize_phone("07701234567") == "07701234567"
    assert normalize_phone("+964 770 123 4567") == "07701234567"
    assert normalize_phone("009647701234567") == "07701234567"
    assert normalize_phone("770-123-4567") == "07701234567"
    assert normalize_phone("٠٧٧٠١٢٣٤٥٦٧") == "07701234567"

    with pytest.raises(ApiException) as exc:
        normalize_phone("12345")
    assert exc.value.code == ErrorCode.VALIDATION


def test_round_250_and_percent_exact():
    assert round_250(0) == 0
    assert round_250(1000) == 1000
    assert round_250(1124) == 1000
    assert round_250(1125) == 1250
    assert round_250(1375) == 1500
    assert calc_percent_round(10000, "2.5") == 250


def test_baghdad_day_cutoff():
    # 2026-05-01 22:00 UTC == 2026-05-02 01:00 Asia/Baghdad
    assert baghdad_day_of("2026-05-01T22:00:00Z") == "2026-05-02"
    assert baghdad_day_of("2026-05-01T20:59:59Z") == "2026-05-01"


def test_pin_encryption_roundtrip():
    token = encrypt_secret("4921")
    assert token != "4921"
    assert decrypt_secret(token) == "4921"


def test_image_decoding_and_validation():
    img = Image.new("RGB", (64, 64), color=(20, 120, 200))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    data_url = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")

    raw, ext, mime = decode_image_data_url(data_url)
    assert ext == "jpg"
    assert mime == "image/jpeg"
    assert len(raw) > 0

    with pytest.raises(ApiException):
        decode_image_data_url("data:image/svg+xml;base64,PHN2Zz48L3N2Zz4=")


@pytest.mark.django_db
def test_sqlite_foreign_keys_enabled():
    if connection.vendor == "sqlite":
        with connection.cursor() as cursor:
            cursor.execute("PRAGMA foreign_keys;")
            assert cursor.fetchone()[0] == 1


@pytest.mark.django_db
def test_health_and_protected_metrics():
    client = APIClient()
    live = client.get("/health/live")
    assert live.status_code == 200
    assert live.json()["status"] == "ok"

    ready = client.get("/health/ready")
    assert ready.status_code == 200
    assert ready.json()["status"] == "ready"

    metrics_denied = client.get("/metrics")
    assert metrics_denied.status_code == 403


@pytest.mark.django_db
def test_local_signed_media_access(tmp_path, settings):
    settings.MEDIA_ROOT = tmp_path
    storage = LocalMediaStorage(root=tmp_path)
    storage.put_bytes("delivery-proofs", "order-1/proof.jpg", b"\xff\xd8\xffdemo", "image/jpeg")

    signed_url = storage.get_url("delivery-proofs", "order-1/proof.jpg", private=True)
    client = APIClient()

    # Unsigned or direct URL cannot bypass private bucket
    assert client.get("/media/delivery-proofs/order-1/proof.jpg").status_code == 403
    assert client.get(signed_url).status_code == 200
    assert client.get(signed_url + "tampered").status_code == 403
