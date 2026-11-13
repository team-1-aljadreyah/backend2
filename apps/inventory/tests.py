"""Phase 3 tests: Catalog, PriceChange history, Storage images, FEFO batches, holds, receiving, redaction."""
from __future__ import annotations

import base64
import io
from datetime import timedelta

import pytest
from django.db import transaction
from PIL import Image
from rest_framework.test import APIClient

from apps.accounts.models import PriceTier, Role, SystemSetting, User
from apps.catalog.models import Item, PriceChange
from apps.common.exceptions import ApiException, ErrorCode
from apps.common.utils import baghdad_today
from apps.customers.models import Customer, CustomerStatus, ShopMembership
from apps.inventory.models import Batch, InventoryMovement
from apps.inventory.services import lock_and_deduct_fefo
from apps.warehouses.models import Warehouse


@pytest.fixture
def phase3_setup(db, tmp_path, settings):
    settings.MEDIA_ROOT = tmp_path
    wh1 = Warehouse.objects.create(name="الصناعة", name_en="Al-Sinaa", code="WH1")
    wh2 = Warehouse.objects.create(name="الحر", name_en="Al-Hur", code="WH2")
    tier1 = PriceTier.objects.create(name="جملة", name_en="Wholesale", sort_order=1)
    tier2 = PriceTier.objects.create(name="مفرد", name_en="Retail", sort_order=2)
    SystemSetting.get_solo()

    manager = User.objects.create_user(phone="07702000001", password="Password123", name="Mgr", role=Role.MANAGER)
    keeper1 = User.objects.create_user(
        phone="07702000002", password="Password123", name="Keeper1", role=Role.WAREHOUSE, warehouse=wh1
    )
    staff1 = User.objects.create_user(
        phone="07702000003", password="Password123", name="Staff1", role=Role.STAFF, warehouse=wh1
    )
    rep = User.objects.create_user(phone="07702000004", password="Password123", name="Rep1", role=Role.REP)
    shop_cust = Customer.objects.create(
        name="Shop A", phone="07702000005", city="Baghdad", area="Karrada", street="St 1", landmark="Near SQ", tier=tier2, status=CustomerStatus.ACTIVE
    )
    shop_user = User.objects.create_user(phone="07702000005", password="Password123", name="ShopOwner", role=Role.SHOP)
    ShopMembership.objects.create(user=shop_user, customer=shop_cust, is_primary=True, is_active_shop=True)

    return {
        "wh1": wh1,
        "wh2": wh2,
        "tier1": tier1,
        "tier2": tier2,
        "manager": manager,
        "keeper1": keeper1,
        "staff1": staff1,
        "rep": rep,
        "shop_user": shop_user,
    }


def _client_for(phone: str) -> APIClient:
    c = APIClient()
    res = c.post("/api/v1/auth/login", {"phone": phone, "password": "Password123"}, format="json")
    assert res.status_code == 200
    c.credentials(HTTP_AUTHORIZATION=f"Bearer {res.json()['data']['token']}")
    return c


def _make_jpeg_data_url() -> str:
    img = Image.new("RGB", (80, 80), color=(40, 160, 80))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


@pytest.mark.django_db
def test_item_crud_price_change_history_and_image_storage(phase3_setup):
    mgr = _client_for("07702000001")
    shop = _client_for("07702000005")
    t1, t2 = phase3_setup["tier1"], phase3_setup["tier2"]

    res = mgr.post(
        "/api/v1/items",
        {
            "name": "زيت نور ١ لتر",
            "name_en": "Noor Oil 1L",
            "barcode": "629000111",
            "category": "زيوت",
            "pieces_per_carton": 12,
            "min_stock": 24,
            "prices": [
                {"tier_id": t1.id, "carton_price": 24000, "piece_price": 2100},
                {"tier_id": t2.id, "carton_price": 26000, "piece_price": 2300},
            ],
        },
        format="json",
    )
    assert res.status_code == 201, res.json()
    item_id = res.json()["data"]["id"]
    assert PriceChange.objects.filter(item_id=item_id).count() == 2

    # Price edit records new PriceChange for changed tier only
    upd = mgr.patch(
        f"/api/v1/items/{item_id}",
        {
            "prices": [
                {"tier_id": t1.id, "carton_price": 24000, "piece_price": 2100},
                {"tier_id": t2.id, "carton_price": 27000, "piece_price": 2400},
            ]
        },
        format="json",
    )
    assert upd.status_code == 200
    assert PriceChange.objects.filter(item_id=item_id, tier=t2).count() == 2
    assert PriceChange.objects.filter(item_id=item_id, tier=t1).count() == 1

    # Upload product image -> stored in StorageBackend, NOT Base64 in DB
    img_res = mgr.post(f"/api/v1/items/{item_id}/image", {"image": _make_jpeg_data_url()}, format="json")
    assert img_res.status_code == 200
    photo_url = img_res.json()["data"]["image"]
    assert photo_url and "/media/product-images/" in photo_url
    db_item = Item.objects.get(id=item_id)
    assert not db_item.image_path.startswith("data:")

    # Shop only sees its tier2 price
    shop_item = shop.get(f"/api/v1/items/{item_id}").json()["data"]
    assert len(shop_item["prices"]) == 1
    assert shop_item["prices"][0]["tier_id"] == t2.id
    assert shop_item["prices"][0]["carton_price"] == 27000


@pytest.mark.django_db
def test_receipts_weighted_avg_cost_fefo_and_role_redaction(phase3_setup):
    mgr = _client_for("07702000001")
    keeper1 = _client_for("07702000002")
    staff1 = _client_for("07702000003")
    rep = _client_for("07702000004")

    t1, t2 = phase3_setup["tier1"], phase3_setup["tier2"]
    wh1, wh2 = phase3_setup["wh1"], phase3_setup["wh2"]
    item_id = mgr.post(
        "/api/v1/items",
        {
            "name": "أرز بسمتي",
            "name_en": "Basmati Rice",
            "category": "حبوب",
            "pieces_per_carton": 10,
            "prices": [
                {"tier_id": t1.id, "carton_price": 20000, "piece_price": 2100},
                {"tier_id": t2.id, "carton_price": 22000, "piece_price": 2300},
            ],
        },
        format="json",
    ).json()["data"]["id"]

    today = baghdad_today()
    exp_near = (today + timedelta(days=20)).isoformat()
    exp_far = (today + timedelta(days=120)).isoformat()
    exp_past = (today - timedelta(days=1)).isoformat()

    # Reject expired goods cleanly with dotted line key
    bad = keeper1.post(
        "/api/v1/receipts",
        {"warehouse_id": wh1.id, "lines": [{"item_id": item_id, "unit": "carton", "qty": 2, "expiry_date": exp_past, "purchase_price": 1500}]},
        format="json",
    )
    assert bad.status_code == 400
    assert "lines.0.expiry_date" in bad.json().get("errors", {})

    # Keeper1 blocked from receiving into wh2
    cross = keeper1.post(
        "/api/v1/receipts",
        {"warehouse_id": wh2.id, "lines": [{"item_id": item_id, "unit": "piece", "qty": 5, "expiry_date": exp_far, "purchase_price": 1500}]},
        format="json",
    )
    assert cross.status_code == 403

    # Receipt 1: 10 pieces at 1000 IQD/pc
    r1 = keeper1.post(
        "/api/v1/receipts",
        {
            "warehouse_id": wh1.id,
            "supplier": "Supplier A",
            "lines": [{"item_id": item_id, "unit": "piece", "qty": 10, "expiry_date": exp_far, "purchase_price": 1000}],
        },
        format="json",
    )
    assert r1.status_code == 201
    # Receipt 2: 1 carton (10 pieces) at 2000 IQD/pc with EARLIER expiry
    r2 = keeper1.post(
        "/api/v1/receipts",
        {
            "warehouse_id": wh1.id,
            "supplier": "Supplier B",
            "lines": [{"item_id": item_id, "unit": "carton", "qty": 1, "expiry_date": exp_near, "purchase_price": 2000}],
        },
        format="json",
    )
    assert r2.status_code == 201
    assert Item.objects.get(id=item_id).avg_cost == 1500

    # Role redaction: staff & rep omit avg_cost while keeper & mgr see 1500
    assert keeper1.get(f"/api/v1/items/{item_id}").json()["data"]["avg_cost"] == 1500
    assert "avg_cost" not in staff1.get(f"/api/v1/items/{item_id}").json()["data"]
    assert "avg_cost" not in rep.get(f"/api/v1/items/{item_id}").json()["data"]
    assert "total_cost" not in staff1.get(f"/api/v1/receipts/{r1.json()['data']['id']}").json()["data"]

    # FEFO deducts earliest expiry batch first (exp_near batch from r2)
    b_far_id = r1.json()["data"]["lines"][0]["batch_id"]
    b_near_id = r2.json()["data"]["lines"][0]["batch_id"]
    with transaction.atomic():
        allocs = lock_and_deduct_fefo(warehouse_id=wh1.id, item_id=item_id, qty_pieces=12)
    assert allocs[0][0].id == b_near_id and allocs[0][1] == 10
    assert allocs[1][0].id == b_far_id and allocs[1][1] == 2
    assert InventoryMovement.objects.filter(item_id=item_id).count() >= 4

    # Insufficient stock rolls back cleanly with OUT_OF_STOCK
    with pytest.raises(ApiException) as exc:
        with transaction.atomic():
            lock_and_deduct_fefo(warehouse_id=wh1.id, item_id=item_id, qty_pieces=50)
    assert exc.value.code == ErrorCode.OUT_OF_STOCK
    assert Batch.objects.get(id=b_far_id).qty_left == 8


@pytest.mark.django_db
def test_batch_hold_and_release_syncs_across_warehouses(phase3_setup):
    mgr = _client_for("07702000001")
    keeper1 = _client_for("07702000002")
    t1 = phase3_setup["tier1"]
    wh1, wh2 = phase3_setup["wh1"], phase3_setup["wh2"]

    item_id = mgr.post(
        "/api/v1/items",
        {
            "name": "شاي أحمد",
            "name_en": "Ahmad Tea",
            "category": "مشروبات",
            "pieces_per_carton": 24,
            "prices": [{"tier_id": t1.id, "carton_price": 48000, "piece_price": 2200}],
        },
        format="json",
    ).json()["data"]["id"]

    exp = (baghdad_today() + timedelta(days=90)).isoformat()
    rec = mgr.post(
        "/api/v1/receipts",
        {"warehouse_id": wh1.id, "lines": [{"item_id": item_id, "unit": "piece", "qty": 20, "expiry_date": exp, "purchase_price": 1000}]},
        format="json",
    ).json()["data"]
    b1 = Batch.objects.get(id=rec["lines"][0]["batch_id"])
    # Simulate sibling batch transferred to wh2 from same lot
    b2 = Batch.objects.create(
        item_id=item_id,
        warehouse=wh2,
        receipt_id=rec["id"],
        origin_batch=b1,
        qty_received=10,
        qty_left=10,
        purchase_price=1000,
        expiry_date=b1.expiry_date,
    )

    hold_res = keeper1.post(f"/api/v1/batches/{b1.id}/hold", {"reason": "Customer complaint"}, format="json")
    assert hold_res.status_code == 200
    b1.refresh_from_db()
    b2.refresh_from_db()
    assert b1.is_held and b2.is_held

    # Keeper cannot release; only manager can
    assert keeper1.post(f"/api/v1/batches/{b1.id}/release", format="json").status_code == 403
    assert mgr.post(f"/api/v1/batches/{b1.id}/release", format="json").status_code == 200
    b1.refresh_from_db()
    b2.refresh_from_db()
    assert not b1.is_held and not b2.is_held
