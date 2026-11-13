"""Tests for Supply Orders CRUD, RBAC, cancellation guards, and partial/full receipt reconciliation."""
from __future__ import annotations

from datetime import timedelta

import pytest
from rest_framework.test import APIClient

from apps.accounts.models import PriceTier, RepKind, Role, SystemSetting, User
from apps.common.utils import baghdad_today
from apps.warehouses.models import Warehouse


def _client(phone: str) -> APIClient:
    user = User.objects.get(phone=phone)
    client = APIClient()
    client.force_authenticate(user=user)
    return client


@pytest.fixture
def supply_setup(db):
    wh1 = Warehouse.objects.create(name="المخزن الرئيسي", name_en="Main WH", code="SW1", active=True)
    wh2 = Warehouse.objects.create(name="المخزن الفرعي", name_en="Sub WH", code="SW2", active=True)
    tier = PriceTier.objects.create(name="جملة", name_en="Wholesale", sort_order=1)
    SystemSetting.get_solo()

    mgr = User.objects.create_user(phone="07710000001", password="Password123", name="Mgr", role=Role.MANAGER)
    acct = User.objects.create_user(phone="07710000002", password="Password123", name="Acct", role=Role.ACCOUNTANT)
    k1 = User.objects.create_user(
        phone="07710000003", password="Password123", name="K1", role=Role.WAREHOUSE, warehouse=wh1
    )
    k2 = User.objects.create_user(
        phone="07710000004", password="Password123", name="K2", role=Role.WAREHOUSE, warehouse=wh2
    )
    staff1 = User.objects.create_user(
        phone="07710000005", password="Password123", name="Staff1", role=Role.STAFF, warehouse=wh1
    )
    rep = User.objects.create_user(
        phone="07710000006", password="Password123", name="Rep", role=Role.REP, rep_kinds=[RepKind.SALES]
    )
    shop = User.objects.create_user(phone="07710000007", password="Password123", name="Shop", role=Role.SHOP)

    m_cli = _client("07710000001")
    it1 = m_cli.post(
        "/api/v1/items",
        {
            "name": "زيت طبخ",
            "name_en": "Cooking Oil",
            "category": "زيوت",
            "pieces_per_carton": 10,
            "prices": [{"tier_id": tier.id, "carton_price": 25000, "piece_price": 2500}],
        },
        format="json",
    ).json()["data"]["id"]
    it2 = m_cli.post(
        "/api/v1/items",
        {
            "name": "سكر أبيض",
            "name_en": "White Sugar",
            "category": "سكر",
            "pieces_per_carton": 12,
            "prices": [{"tier_id": tier.id, "carton_price": 15000, "piece_price": 1500}],
        },
        format="json",
    ).json()["data"]["id"]
    return {
        "wh1": wh1,
        "wh2": wh2,
        "mgr": mgr,
        "acct": acct,
        "k1": k1,
        "k2": k2,
        "staff1": staff1,
        "rep": rep,
        "shop": shop,
        "it1": it1,
        "it2": it2,
    }


@pytest.mark.django_db
def test_supply_order_rbac_validation_and_cancel(supply_setup):
    mgr = _client("07710000001")
    acct = _client("07710000002")
    k1 = _client("07710000003")
    k2 = _client("07710000004")
    staff1 = _client("07710000005")
    rep = _client("07710000006")
    shop = _client("07710000007")
    wh1, wh2, it1 = supply_setup["wh1"], supply_setup["wh2"], supply_setup["it1"]

    for forbidden in (rep, shop):
        assert forbidden.get("/api/v1/supply-orders").status_code == 403
        assert (
            forbidden.post(
                "/api/v1/supply-orders",
                {"warehouse_id": wh1.id, "supplier": "X", "lines": [{"item_id": it1, "unit": "carton", "qty": 1, "price": 1000}]},
                format="json",
            ).status_code
            == 403
        )

    assert (
        k1.post(
            "/api/v1/supply-orders",
            {"warehouse_id": wh1.id, "supplier": "X", "lines": [{"item_id": it1, "unit": "carton", "qty": 1, "price": 1000}]},
            format="json",
        ).status_code
        == 403
    )

    past = (baghdad_today() - timedelta(days=2)).isoformat()
    bad = mgr.post(
        "/api/v1/supply-orders",
        {
            "warehouse_id": wh1.id,
            "supplier": "",
            "expected_date": past,
            "lines": [],
        },
        format="json",
    )
    assert bad.status_code == 400 and bad.json()["code"] == "VALIDATION"
    errs = bad.json()["errors"]
    assert "supplier" in errs and "lines" in errs and "expected_date" in errs

    so_wh2 = acct.post(
        "/api/v1/supply-orders",
        {
            "warehouse_id": wh2.id,
            "supplier": "مورد الشمال",
            "supplier_phone": "07715556677",
            "expected_date": (baghdad_today() + timedelta(days=3)).isoformat(),
            "lines": [{"item_id": it1, "unit": "carton", "qty": 4, "price": 20000}],
        },
        format="json",
    )
    assert so_wh2.status_code == 201
    so2_id = so_wh2.json()["data"]["id"]
    assert so_wh2.json()["data"]["status"] == "sent"
    assert so_wh2.json()["data"]["total"] == 80000

    # Warehouse isolation: k1 (WH1) and staff1 (WH1) cannot view WH2 supply order (404 IDOR-safe); k2 (WH2) can
    assert k1.get(f"/api/v1/supply-orders/{so2_id}").status_code == 404
    assert staff1.get(f"/api/v1/supply-orders/{so2_id}").status_code == 404
    assert k2.get(f"/api/v1/supply-orders/{so2_id}").status_code == 200
    assert k1.get("/api/v1/supply-orders").json()["data"] == []
    assert len(k2.get("/api/v1/supply-orders").json()["data"]) == 1

    # Keeper cannot cancel; manager can cancel with non-empty reason
    assert k2.post(f"/api/v1/supply-orders/{so2_id}/cancel", {"reason": "no"}, format="json").status_code == 403
    assert mgr.post(f"/api/v1/supply-orders/{so2_id}/cancel", {"reason": "   "}, format="json").status_code == 400
    cancelled = mgr.post(f"/api/v1/supply-orders/{so2_id}/cancel", {"reason": "تأخر المورد"}, format="json")
    assert cancelled.status_code == 200
    assert cancelled.json()["data"]["status"] == "cancelled"
    assert cancelled.json()["data"]["cancel_reason"] == "تأخر المورد"


@pytest.mark.django_db
def test_supply_order_partial_and_full_receipt_reconciliation(supply_setup):
    mgr = _client("07710000001")
    k1 = _client("07710000003")
    wh1, it1, it2 = supply_setup["wh1"], supply_setup["it1"], supply_setup["it2"]
    exp = (baghdad_today() + timedelta(days=90)).isoformat()

    so = mgr.post(
        "/api/v1/supply-orders",
        {
            "warehouse_id": wh1.id,
            "supplier": "مورد الجنوب",
            "lines": [
                {"item_id": it1, "unit": "carton", "qty": 10, "price": 20000},
                {"item_id": it2, "unit": "carton", "qty": 5, "price": 12000},
            ],
        },
        format="json",
    ).json()["data"]
    so_id = so["id"]
    assert so["total"] == 260000

    # Partial receipt against supply_order_id -> status becomes "partial"
    r1 = k1.post(
        "/api/v1/receipts",
        {
            "warehouse_id": wh1.id,
            "supply_order_id": so_id,
            "lines": [{"item_id": it1, "unit": "carton", "qty": 6, "expiry_date": exp, "purchase_price": 2000}],
        },
        format="json",
    )
    assert r1.status_code == 201
    assert r1.json()["data"]["supply_order_id"] == so_id

    after_partial = mgr.get(f"/api/v1/supply-orders/{so_id}").json()["data"]
    assert after_partial["status"] == "partial"
    assert after_partial["receipt_ids"] == [r1.json()["data"]["id"]]
    assert after_partial["lines"][0]["received_qty"] == 6
    assert after_partial["lines"][1]["received_qty"] == 0

    # Partially received supply order cannot be cancelled
    assert mgr.post(f"/api/v1/supply-orders/{so_id}/cancel", {"reason": "Late"}, format="json").status_code == 400

    # Complete remaining items (4 cartons it1 + 5 cartons it2) -> status becomes "received"
    r2 = k1.post(
        "/api/v1/receipts",
        {
            "warehouse_id": wh1.id,
            "supply_order_id": so_id,
            "lines": [
                {"item_id": it1, "unit": "carton", "qty": 4, "expiry_date": exp, "purchase_price": 2000},
                {"item_id": it2, "unit": "carton", "qty": 5, "expiry_date": exp, "purchase_price": 1000},
            ],
        },
        format="json",
    )
    assert r2.status_code == 201

    after_full = mgr.get(f"/api/v1/supply-orders/{so_id}").json()["data"]
    assert after_full["status"] == "received"
    assert after_full["receipt_ids"] == [r1.json()["data"]["id"], r2.json()["data"]["id"]]
    assert after_full["lines"][0]["received_qty"] == 10
    assert after_full["lines"][1]["received_qty"] == 5
