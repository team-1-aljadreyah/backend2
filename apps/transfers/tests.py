"""Phase 4 tests: Transfers (FEFO dispatch, partial receive, review, Idempotency-Key) and Stocktakes."""
from __future__ import annotations

from datetime import timedelta

import pytest
from rest_framework.test import APIClient

from apps.accounts.models import PriceTier, Role, SystemSetting, User
from apps.common.utils import baghdad_today
from apps.inventory.models import Batch, DamagedInventory
from apps.inventory.services import is_count_locked
from apps.warehouses.models import Warehouse


@pytest.fixture
def phase4_setup(db):
    wh1 = Warehouse.objects.create(name="الصناعة", name_en="Al-Sinaa", code="S1")
    wh2 = Warehouse.objects.create(name="الحر", name_en="Al-Hur", code="H2")
    tier = PriceTier.objects.create(name="جملة", name_en="Wholesale", sort_order=1)
    SystemSetting.get_solo()

    mgr = User.objects.create_user(phone="07703000001", password="Password123", name="Mgr", role=Role.MANAGER)
    k1 = User.objects.create_user(phone="07703000002", password="Password123", name="Keeper1", role=Role.WAREHOUSE, warehouse=wh1)
    k2 = User.objects.create_user(phone="07703000003", password="Password123", name="Keeper2", role=Role.WAREHOUSE, warehouse=wh2)
    staff1 = User.objects.create_user(phone="07703000004", password="Password123", name="Staff1", role=Role.STAFF, warehouse=wh1)
    return {"wh1": wh1, "wh2": wh2, "tier": tier, "mgr": mgr, "k1": k1, "k2": k2, "staff1": staff1}


def _cli(phone: str) -> APIClient:
    c = APIClient()
    tok = c.post("/api/v1/auth/login", {"phone": phone, "password": "Password123"}, format="json").json()["data"]["token"]
    c.credentials(HTTP_AUTHORIZATION=f"Bearer {tok}")
    return c


@pytest.mark.django_db
def test_transfer_idempotency_discrepancy_and_lineage(phase4_setup):
    mgr = _cli("07703000001")
    k1 = _cli("07703000002")
    k2 = _cli("07703000003")
    wh1, wh2, tier = phase4_setup["wh1"], phase4_setup["wh2"], phase4_setup["tier"]

    item_id = mgr.post(
        "/api/v1/items",
        {
            "name": "حليب نيدو",
            "name_en": "Nido Milk",
            "category": "ألبان",
            "pieces_per_carton": 12,
            "prices": [{"tier_id": tier.id, "carton_price": 60000, "piece_price": 5200}],
        },
        format="json",
    ).json()["data"]["id"]

    exp = (baghdad_today() + timedelta(days=90)).isoformat()
    rec = k1.post(
        "/api/v1/receipts",
        {"warehouse_id": wh1.id, "lines": [{"item_id": item_id, "unit": "piece", "qty": 30, "expiry_date": exp, "purchase_price": 4000}]},
        format="json",
    ).json()["data"]
    src_batch_id = rec["lines"][0]["batch_id"]

    payload = {"from_warehouse_id": wh1.id, "to_warehouse_id": wh2.id, "reason": "Rebalance", "lines": [{"item_id": item_id, "pieces": 20}]}
    t1 = k1.post("/api/v1/transfers", payload, format="json", HTTP_IDEMPOTENCY_KEY="trf-key-001")
    assert t1.status_code == 201
    # Retry with same Idempotency-Key returns same transfer without deducting twice!
    t_dup = k1.post("/api/v1/transfers", payload, format="json", HTTP_IDEMPOTENCY_KEY="trf-key-001")
    assert t_dup.status_code == 201
    assert t_dup.json()["data"]["id"] == t1.json()["data"]["id"]
    assert Batch.objects.get(id=src_batch_id).qty_left == 10

    trf_id = t1.json()["data"]["id"]
    # Partial receive without reason rejected
    bad_recv = k2.post(f"/api/v1/transfers/{trf_id}/receive", {"received": {str(item_id): 18}, "reason": ""}, format="json")
    assert bad_recv.status_code == 400

    # Valid discrepancy receive
    recv = k2.post(
        f"/api/v1/transfers/{trf_id}/receive",
        {"received": {str(item_id): 18}, "reason": "2 cans dented in transit"},
        format="json",
    )
    assert recv.status_code == 200
    assert recv.json()["data"]["status"] == "discrepancy"
    dest_batch = Batch.objects.get(warehouse=wh2, item_id=item_id)
    assert dest_batch.qty_left == 18
    assert dest_batch.origin_batch_id == src_batch_id
    assert dest_batch.expiry_date.isoformat() == exp

    # Holding source batch also holds transferred destination batch
    assert k1.post(f"/api/v1/batches/{src_batch_id}/hold", {"reason": "Lab check"}, format="json").status_code == 200
    dest_batch.refresh_from_db()
    assert dest_batch.is_held

    # Non-manager cannot review discrepancy; manager can
    assert k2.post(f"/api/v1/transfers/{trf_id}/review", format="json").status_code == 403
    rev = mgr.post(f"/api/v1/transfers/{trf_id}/review", format="json")
    assert rev.status_code == 200
    assert rev.json()["data"]["status"] == "received"


@pytest.mark.django_db
def test_stocktake_locking_staff_restriction_and_close_adjustment(phase4_setup):
    mgr = _cli("07703000001")
    k1 = _cli("07703000002")
    staff1 = _cli("07703000004")
    wh1, wh2, tier = phase4_setup["wh1"], phase4_setup["wh2"], phase4_setup["tier"]

    i_oil = mgr.post(
        "/api/v1/items",
        {"name": "زيت", "name_en": "Oil", "category": "زيوت", "pieces_per_carton": 10, "prices": [{"tier_id": tier.id, "carton_price": 10000, "piece_price": 1000}]},
        format="json",
    ).json()["data"]["id"]
    i_tea = mgr.post(
        "/api/v1/items",
        {"name": "شاي", "name_en": "Tea", "category": "مشروبات", "pieces_per_carton": 10, "prices": [{"tier_id": tier.id, "carton_price": 10000, "piece_price": 1000}]},
        format="json",
    ).json()["data"]["id"]

    exp = (baghdad_today() + timedelta(days=60)).isoformat()
    k1.post(
        "/api/v1/receipts",
        {
            "warehouse_id": wh1.id,
            "lines": [
                {"item_id": i_oil, "unit": "piece", "qty": 25, "expiry_date": exp, "purchase_price": 800},
                {"item_id": i_tea, "unit": "piece", "qty": 15, "expiry_date": exp, "purchase_price": 700},
            ],
        },
        format="json",
    )

    # Staff cannot create or close stocktakes; warehouse keeper & manager manage counts
    assert staff1.post("/api/v1/stocktakes", {"warehouse_id": wh1.id, "category": "زيوت"}, format="json").status_code == 403
    st_res = k1.post("/api/v1/stocktakes", {"warehouse_id": wh1.id, "category": "زيوت"}, format="json")
    assert st_res.status_code == 201
    st_id = st_res.json()["data"]["id"]
    assert is_count_locked(wh1.id, "زيوت") is True
    assert is_count_locked(wh1.id, "مشروبات") is False
    assert is_count_locked(wh2.id, "زيوت") is False

    # Transfers in locked category blocked cleanly
    blocked_trf = k1.post(
        "/api/v1/transfers",
        {"from_warehouse_id": wh1.id, "to_warehouse_id": wh2.id, "lines": [{"item_id": i_oil, "pieces": 5}]},
        format="json",
    )
    assert blocked_trf.status_code == 400
    assert blocked_trf.json()["code"] == "BAD_STATE"

    # Staff cannot edit stocktake lines; keeper records counted shortage of 3 without reason -> close fails
    assert staff1.patch(
        f"/api/v1/stocktakes/{st_id}",
        {"lines": [{"item_id": i_oil, "counted_qty": 22, "damaged_qty": 3, "reason": ""}]},
        format="json",
    ).status_code == 403
    k1.patch(
        f"/api/v1/stocktakes/{st_id}",
        {"lines": [{"item_id": i_oil, "counted_qty": 22, "damaged_qty": 3, "reason": ""}]},
        format="json",
    )
    assert staff1.post(f"/api/v1/stocktakes/{st_id}/close", format="json").status_code == 403
    assert k1.post(f"/api/v1/stocktakes/{st_id}/close", format="json").status_code == 400

    # Add reason and close by keeper
    k1.patch(
        f"/api/v1/stocktakes/{st_id}",
        {"lines": [{"item_id": i_oil, "counted_qty": 22, "damaged_qty": 3, "reason": "3 broken bottles"}]},
        format="json",
    )
    close_res = k1.post(f"/api/v1/stocktakes/{st_id}/close", format="json")
    assert close_res.status_code == 200
    assert close_res.json()["data"]["status"] == "closed"
    assert is_count_locked(wh1.id, "زيوت") is False
    assert Batch.objects.get(warehouse=wh1, item_id=i_oil).qty_left == 22
    assert DamagedInventory.objects.filter(source="stocktake", item_id=i_oil, pieces=3).exists()
