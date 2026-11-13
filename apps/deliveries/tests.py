"""Tests for Deliveries: QR dispatch, 5-attempt persistent PIN lock, proof photo, partial split, problem recovery."""
from __future__ import annotations

import base64
import io
from datetime import timedelta

import pytest
from PIL import Image
from rest_framework.test import APIClient

from apps.accounts.models import PriceTier, RepKind, Role, User
from apps.common.utils import baghdad_today
from apps.customers.models import Customer, CustomerStatus, ShopMembership
from apps.inventory.models import Batch
from apps.invoices.models import Invoice
from apps.notifications.models import Notification
from apps.orders.models import OrderStatus
from apps.warehouses.models import Warehouse


def _c(phone: str) -> APIClient:
    user = User.objects.get(phone=phone)
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def _sample_jpeg_data_url() -> str:
    img = Image.new("RGB", (60, 60), color=(40, 120, 200))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


@pytest.fixture
def phase7_setup(db):
    wh = Warehouse.objects.create(name="المخزن", name_en="Main", code="W1", active=True)
    tier = PriceTier.objects.create(name="جملة", name_en="Wholesale", sort_order=1)
    mgr = User.objects.create_user("07707000001", "Pass1234", name="Mgr", role=Role.MANAGER)
    keeper = User.objects.create_user("07707000002", "Pass1234", name="Keeper", role=Role.WAREHOUSE, warehouse=wh)
    rep = User.objects.create_user("07707000003", "Pass1234", name="Sales", role=Role.REP, rep_kinds=[RepKind.SALES])
    d1 = User.objects.create_user("07707000004", "Pass1234", name="Driver1", role=Role.REP, rep_kinds=[RepKind.DELIVERY])
    d2 = User.objects.create_user("07707000005", "Pass1234", name="Driver2", role=Role.REP, rep_kinds=[RepKind.DELIVERY])
    shop_u = User.objects.create_user("07707000006", "Pass1234", name="Shop", role=Role.SHOP)

    cust = Customer.objects.create(
        name="محل السلام",
        owner_name="Shop",
        phone="07707000006",
        city="بغداد",
        area="المنصور",
        street="14",
        landmark="جسر",
        tier=tier,
        rep=rep,
        debt_limit=2_000_000,
        status=CustomerStatus.ACTIVE,
        active=True,
    )
    ShopMembership.objects.create(user=shop_u, customer=cust, is_primary=True, is_active_shop=True)

    return {"wh": wh, "tier": tier, "mgr": mgr, "keeper": keeper, "rep": rep, "d1": d1, "d2": d2, "shop_u": shop_u, "cust": cust}


@pytest.mark.django_db
def test_delivery_qr_dispatch_5try_pin_lock_photo_security_and_partial_split(phase7_setup):
    mgr = _c("07707000001")
    keeper = _c("07707000002")
    d1 = _c("07707000004")
    d2 = _c("07707000005")
    shop = _c("07707000006")

    wh, tier, cust = phase7_setup["wh"], phase7_setup["tier"], phase7_setup["cust"]
    exp = (baghdad_today() + timedelta(days=90)).isoformat()

    item_id = mgr.post(
        "/api/v1/items",
        {"name": "حليب", "name_en": "Milk", "category": "ألبان", "pieces_per_carton": 10, "prices": [{"tier_id": tier.id, "carton_price": 20000, "piece_price": 2000}]},
        format="json",
    ).json()["data"]["id"]
    keeper.post(
        "/api/v1/receipts",
        {"warehouse_id": wh.id, "lines": [{"item_id": item_id, "unit": "carton", "qty": 10, "expiry_date": exp, "purchase_price": 1500}]},
        format="json",
    )

    order = mgr.post(
        "/api/v1/orders",
        {"customer_id": cust.id, "warehouse_id": wh.id, "lines": [{"item_id": item_id, "unit": "carton", "qty": 5}]},
        format="json",
    ).json()["data"]
    line_id = order["lines"][0]["id"]

    # Keeper prepares -> creates Delivery ready with code & secret PIN
    prepped = keeper.post(
        f"/api/v1/orders/{order['id']}/prepare",
        {"warehouses": {str(line_id): wh.id}},
        format="json",
    ).json()["data"]
    code = prepped["delivery"]["code"]

    # QR Dispatch with NAHRAIN:DLV: prefix & hyphens
    disp = d1.post("/api/v1/deliveries/dispatch", {"code": f"NAHRAIN:DLV:{code[:4]}-{code[4:]}"}, format="json")
    assert disp.status_code == 200
    disp_data = disp.json()["data"]
    assert disp_data["delivery"]["status"] == "out"
    assert disp_data["delivery"]["pin"] is None  # Driver never sees PIN!

    # Shop sees its PIN and received dispatch notification
    shop_order = shop.get(f"/api/v1/orders/{order['id']}").json()["data"]
    pin = shop_order["delivery"]["pin"]
    assert pin and len(pin) == 4
    assert Notification.objects.filter(user=phase7_setup["shop_u"], kind="out_for_delivery").exists()

    # Wrong driver cannot deliver
    assert d2.post(f"/api/v1/orders/{order['id']}/deliver", {"pin": pin, "photo": _sample_jpeg_data_url()}, format="json").status_code == 403

    # 5 invalid PIN attempts persist and lock on the 5th!
    bad_pin = "9999" if pin != "9999" else "0000"
    for attempt in range(1, 6):
        res = d1.post(f"/api/v1/orders/{order['id']}/deliver", {"pin": bad_pin}, format="json")
        assert res.status_code == 200
        body = res.json()["data"]
        assert body["ok"] is False
        assert body["left"] == max(0, 5 - attempt)
        assert body["locked"] is (attempt == 5)

    # Even valid PIN fails while locked
    locked_res = d1.post(f"/api/v1/orders/{order['id']}/deliver", {"pin": pin, "photo": _sample_jpeg_data_url()}, format="json")
    assert locked_res.json()["data"] == {"ok": False, "left": 0, "locked": True}

    # Only manager unlocks
    assert d1.post(f"/api/v1/orders/{order['id']}/delivery/unlock").status_code == 403
    unlocked = mgr.post(f"/api/v1/orders/{order['id']}/delivery/unlock")
    assert unlocked.status_code == 200 and unlocked.json()["data"]["delivery"]["locked"] is False

    # Partial delivery: deliver 3 of 5 cartons and rest="keep" -> spawns approved rest child order for 2 cartons!
    ok_res = d1.post(
        f"/api/v1/orders/{order['id']}/deliver",
        {
            "pin": pin,
            "delivered": {str(line_id): 3},
            "rest": "keep",
            "photo": _sample_jpeg_data_url(),
        },
        format="json",
    )
    assert ok_res.status_code == 200
    res_data = ok_res.json()["data"]
    assert res_data["ok"] is True
    delivered_order = res_data["order"]
    assert delivered_order["status"] == OrderStatus.CONFIRMED
    assert delivered_order["invoice_id"] is not None
    assert delivered_order["rest_order_id"] is not None

    inv = Invoice.objects.get(id=delivered_order["invoice_id"])
    assert inv.total == 60000  # 3 * 20,000
    assert Batch.objects.get(item_id=item_id, warehouse=wh).qty_left == 70  # 100 - 30 pieces

    child = mgr.get(f"/api/v1/orders/{delivered_order['rest_order_id']}").json()["data"]
    assert child["status"] == OrderStatus.APPROVED and child["parent_id"] == order["id"] and child["lines"][0]["qty"] == 2

    # Proof photo private signed URL accessible via endpoint; raw /media/delivery-proofs/ is forbidden
    photo_url = mgr.get(f"/api/v1/orders/{order['id']}/delivery/photo").json()["data"]
    assert "/api/v1/media/signed?token=" in photo_url
    assert mgr.get(photo_url).status_code == 200
    assert d2.get(f"/api/v1/orders/{order['id']}/delivery/photo").status_code == 403


@pytest.mark.django_db
def test_delivery_problem_report_and_return_to_warehouse_re_dispatch(phase7_setup):
    mgr = _c("07707000001")
    keeper = _c("07707000002")
    d1 = _c("07707000004")
    d2 = _c("07707000005")
    wh, tier, cust = phase7_setup["wh"], phase7_setup["tier"], phase7_setup["cust"]
    exp = (baghdad_today() + timedelta(days=90)).isoformat()

    item_id = mgr.post(
        "/api/v1/items",
        {"name": "سكر", "name_en": "Sugar", "category": "سكر", "pieces_per_carton": 10, "prices": [{"tier_id": tier.id, "carton_price": 15000, "piece_price": 1500}]},
        format="json",
    ).json()["data"]["id"]
    keeper.post(
        "/api/v1/receipts",
        {"warehouse_id": wh.id, "lines": [{"item_id": item_id, "unit": "carton", "qty": 5, "expiry_date": exp, "purchase_price": 1000}]},
        format="json",
    )
    order = mgr.post(
        "/api/v1/orders",
        {"customer_id": cust.id, "warehouse_id": wh.id, "lines": [{"item_id": item_id, "unit": "carton", "qty": 2}]},
        format="json",
    ).json()["data"]
    line_id = order["lines"][0]["id"]
    prepped = keeper.post(f"/api/v1/orders/{order['id']}/prepare", {"warehouses": {str(line_id): wh.id}}, format="json").json()["data"]
    code = prepped["delivery"]["code"]

    d1.post("/api/v1/deliveries/dispatch", {"code": code}, format="json")

    # Report "shop_closed" -> stays "out" with structured problem info
    prob = d1.post("/api/v1/deliveries/problem", {"order_ids": [order["id"]], "reason": "shop_closed", "note": "Closed until 5PM"}, format="json")
    assert prob.status_code == 200
    d_state = prob.json()["data"][0]["delivery"]
    assert d_state["status"] == "out"
    assert d_state["problem"]["reason"] == "shop_closed"
    assert d_state["problem"]["by"]["id"] == phase7_setup["d1"].id

    # Warehouse keeper returns shipment to warehouse -> status resets to "ready", rep cleared
    ret_wh = keeper.post(f"/api/v1/orders/{order['id']}/delivery/return")
    assert ret_wh.status_code == 200
    r_state = ret_wh.json()["data"]["delivery"]
    assert r_state["status"] == "ready" and r_state["rep_id"] is None and r_state["problem"] is None

    # Another driver can now dispatch via QR scan
    d2_disp = d2.post("/api/v1/deliveries/dispatch", {"code": code}, format="json")
    assert d2_disp.status_code == 200 and d2_disp.json()["data"]["delivery"]["rep_id"] == phase7_setup["d2"].id
