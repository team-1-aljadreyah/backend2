"""Tests for Multi-Shop Session Isolation, <=10 Shop Limit, Lookups, and Rep Live GPS Endpoints."""
from __future__ import annotations

import base64
import io
from datetime import timedelta
from decimal import Decimal

import pytest
from PIL import Image
from rest_framework.test import APIClient

from apps.accounts.models import PriceTier, RepKind, Role, SystemSetting, User
from apps.common.utils import baghdad_today, decrypt_secret
from apps.customers.models import Customer, CustomerStatus, ShopMembership
from apps.deliveries.models import Delivery, RepLocation
from apps.warehouses.models import Warehouse


def _login(phone: str, password: str = "Password123") -> APIClient:
    cli = APIClient()
    res = cli.post("/api/v1/auth/login", {"phone": phone, "password": password}, format="json")
    assert res.status_code == 200
    tok = res.json()["data"]["token"]
    cli.credentials(HTTP_AUTHORIZATION=f"Bearer {tok}")
    return cli


@pytest.fixture
def multishop_setup(db):
    wh = Warehouse.objects.create(
        name="مخزن كربلاء",
        name_en="Karbala WH",
        code="KWH1",
        capacity=1200,
        lat=Decimal("32.616030"),
        lng=Decimal("44.024880"),
        active=True,
    )
    tier = PriceTier.objects.create(name="جملة", name_en="Wholesale", sort_order=1)
    SystemSetting.get_solo()

    mgr = User.objects.create_user(phone="07720000001", password="Password123", name="Manager", role=Role.MANAGER)
    rep1 = User.objects.create_user(
        phone="07720000002",
        password="Password123",
        name="Rep One",
        role=Role.REP,
        rep_kinds=[RepKind.SALES, RepKind.DELIVERY],
    )
    rep2 = User.objects.create_user(
        phone="07720000003",
        password="Password123",
        name="Rep Two",
        role=Role.REP,
        rep_kinds=[RepKind.DELIVERY],
    )
    shop_user = User.objects.create_user(phone="07720000010", password="Password123", name="أبو محمد", role=Role.SHOP)

    cust_a = Customer.objects.create(
        name="أسواق النور",
        owner_name="أبو محمد",
        phone="07720000010",
        city="كربلاء",
        area="حي الحسين",
        street="10",
        landmark="المستشفى",
        tier=tier,
        rep=rep1,
        debt_limit=500000,
        status=CustomerStatus.ACTIVE,
        active=True,
    )
    cust_b = Customer.objects.create(
        name="أسواق النور فرع 2",
        owner_name="أبو محمد",
        phone="07720000011",
        city="كربلاء",
        area="حي العباس",
        street="20",
        landmark="السوق",
        tier=tier,
        rep=rep2,
        debt_limit=300000,
        status=CustomerStatus.ACTIVE,
        active=True,
    )
    ShopMembership.objects.create(user=shop_user, customer=cust_a, is_primary=True, is_active_shop=True)
    ShopMembership.objects.create(user=shop_user, customer=cust_b, is_primary=False, is_active_shop=False)

    return {"wh": wh, "tier": tier, "mgr": mgr, "rep1": rep1, "rep2": rep2, "shop_user": shop_user, "cust_a": cust_a, "cust_b": cust_b}


@pytest.mark.django_db
def test_multishop_concurrent_session_isolation_limit_and_lookups(multishop_setup):
    cust_a, cust_b = multishop_setup["cust_a"], multishop_setup["cust_b"]

    # Two independent logins by the same shop user maintain independent active_customer_id
    sess1 = _login("07720000010")
    sess2 = _login("07720000010")

    assert sess1.get("/api/v1/me").json()["data"]["customer_id"] == cust_a.id
    assert sess2.get("/api/v1/me").json()["data"]["customer_id"] == cust_a.id

    # Switch only session 1 to cust_b
    sw = sess1.post("/api/v1/me/shop", {"customer_id": cust_b.id}, format="json")
    assert sw.status_code == 200
    assert sw.json()["data"]["customer_id"] == cust_b.id

    # Session 1 now sees cust_b while Session 2 remains isolated on cust_a!
    assert sess1.get("/api/v1/me").json()["data"]["customer_id"] == cust_b.id
    assert sess2.get("/api/v1/me").json()["data"]["customer_id"] == cust_a.id

    # Chat isolation follows active session customer
    sess1.post("/api/v1/chat/me", {"text": "رسالة فرع ب"}, format="json")
    sess2_msgs = sess2.get("/api/v1/chat/me").json()["data"]["messages"]
    assert sess2_msgs == []
    sess1_msgs = sess1.get("/api/v1/chat/me").json()["data"]["messages"]
    assert len(sess1_msgs) == 1 and sess1_msgs[0]["text"] == "رسالة فرع ب"

    # Invalid partial address_info returns clean 400 VALIDATION (never 500!)
    bad_addr = sess1.post(
        "/api/v1/me/shops",
        {
            "shop_name": "فرع غير مكتمل",
            "area": "البهادلية",
            "address_info": {"label": "فرع", "details": ""},
        },
        format="json",
    )
    assert bad_addr.status_code == 400 and bad_addr.json()["code"] == "VALIDATION"
    assert "address_info.details" in bad_addr.json()["errors"]
    assert "address_info.phone" in bad_addr.json()["errors"]

    # Adding additional shops up to 10 succeeds, clean_shop_address computes address_text & coords, 11th rejected
    for idx in range(3, 11):
        res = sess1.post(
            "/api/v1/me/shops",
            {
                "shop_name": f"فرع رقم {idx}",
                "area": "البهادلية",
                "address_info": {
                    "label": f"فرع رقم {idx}",
                    "details": f"مقابل جامع رقم {idx}",
                    "phone": "07720000010",
                    "street": f"شارع {idx}",
                    "lat": 32.616 + idx * 0.001,
                    "lng": 44.024,
                    "extra_ignored": "strip_me",
                },
            },
            format="json",
        )
        assert res.status_code == 201
        assert res.json()["data"]["pending"] is True

    over = sess1.post(
        "/api/v1/me/shops",
        {"shop_name": "فرع حادي عشر", "area": "البهادلية", "street": "11", "landmark": "X"},
        format="json",
    )
    assert over.status_code == 400 and over.json()["code"] == "VALIDATION"

    # Verify clean_shop_address sanitized stored address_info on created customer
    b3 = Customer.objects.get(name="فرع رقم 3")
    assert "extra_ignored" not in (b3.address_info or {})
    assert b3.address_text

    # /api/v1/lookups returns warehouses (with capacity/coords), tiers, settings
    lk = sess1.get("/api/v1/lookups")
    assert lk.status_code == 200
    lkp = lk.json()["data"]
    assert len(lkp["warehouses"]) >= 1
    assert lkp["warehouses"][0]["capacity"] == 1200
    assert lkp["tiers"][0]["id"] == multishop_setup["tier"].id


@pytest.mark.django_db
def test_rep_live_gps_endpoints_and_active_delivery_shop_visibility(multishop_setup):
    mgr = _login("07720000001")
    rep1 = _login("07720000002")
    rep2 = _login("07720000003")
    shop_cli = _login("07720000010")
    wh, tier, cust_a = multishop_setup["wh"], multishop_setup["tier"], multishop_setup["cust_a"]

    # Non-reps cannot POST GPS location; invalid coords fail validation
    assert shop_cli.post("/api/v1/deliveries/location", {"lat": 33.3, "lng": 44.3}, format="json").status_code == 403
    assert mgr.post("/api/v1/deliveries/location", {"lat": 33.3, "lng": 44.3}, format="json").status_code == 403
    assert rep1.post("/api/v1/deliveries/location", {"lat": 99.0, "lng": 44.3}, format="json").status_code == 400
    # Reps without an active OUT delivery cannot publish GPS location (BAD_STATE)
    no_dlv = rep1.post("/api/v1/deliveries/location", {"lat": 32.615, "lng": 44.025}, format="json")
    assert no_dlv.status_code == 400 and no_dlv.json()["code"] == "BAD_STATE"

    # Create item, stock, two orders (cust_a & cust_b), prepare & dispatch via rep1 and rep2
    exp = (baghdad_today() + timedelta(days=90)).isoformat()
    it = mgr.post(
        "/api/v1/items",
        {"name": "تمر", "name_en": "Dates", "category": "تمور", "pieces_per_carton": 10, "prices": [{"tier_id": tier.id, "carton_price": 10000, "piece_price": 1000}]},
        format="json",
    ).json()["data"]["id"]
    mgr.post(
        "/api/v1/receipts",
        {"warehouse_id": wh.id, "lines": [{"item_id": it, "unit": "carton", "qty": 5, "expiry_date": exp, "purchase_price": 600}]},
        format="json",
    )
    ord_res = mgr.post(
        "/api/v1/orders",
        {"customer_id": cust_a.id, "warehouse_id": wh.id, "lines": [{"item_id": it, "unit": "carton", "qty": 1}]},
        format="json",
    ).json()["data"]
    ord_b = mgr.post(
        "/api/v1/orders",
        {"customer_id": multishop_setup["cust_b"].id, "warehouse_id": wh.id, "lines": [{"item_id": it, "unit": "carton", "qty": 1}]},
        format="json",
    ).json()["data"]
    mgr.post(f"/api/v1/orders/{ord_res['id']}/prepare", {"warehouses": {}}, format="json")
    mgr.post(f"/api/v1/orders/{ord_b['id']}/prepare", {"warehouses": {}}, format="json")
    dlv = Delivery.objects.get(order_id=ord_res["id"])
    dlv2 = Delivery.objects.get(order_id=ord_b["id"])
    rep1.post("/api/v1/deliveries/dispatch", {"code": f"NAHRAIN:DLV:{dlv.code}"}, format="json")
    rep2.post("/api/v1/deliveries/dispatch", {"code": f"NAHRAIN:DLV:{dlv2.code}"}, format="json")

    # Both active delivery reps publish GPS; manager sees both; shop sees ONLY rep1's location (not rep2's!)
    assert rep1.post("/api/v1/deliveries/location", {"lat": 32.615, "lng": 44.025}, format="json").status_code == 200
    assert rep2.post("/api/v1/deliveries/location", {"lat": 32.620, "lng": 44.030}, format="json").status_code == 200
    assert len(mgr.get("/api/v1/deliveries/locations").json()["data"]) == 2
    assert len(rep1.get("/api/v1/deliveries/locations").json()["data"]) == 1

    shop_locs = shop_cli.get("/api/v1/deliveries/locations").json()["data"]
    assert len(shop_locs) == 1
    assert shop_locs[0]["rep_id"] == multishop_setup["rep1"].id

    # Stale timestamp (> 10 minutes ago) excluded
    old_ts = RepLocation.objects.get(rep=multishop_setup["rep1"]).updated_at - timedelta(minutes=11)
    RepLocation.objects.filter(rep=multishop_setup["rep1"]).update(updated_at=old_ts)
    assert shop_cli.get("/api/v1/deliveries/locations").json()["data"] == []

    # Refresh location & complete delivery -> visibility ends for shop once OUT ends
    rep1.post("/api/v1/deliveries/location", {"lat": 32.616, "lng": 44.026}, format="json")
    pin = decrypt_secret(dlv.pin_encrypted)
    buf = io.BytesIO()
    Image.new("RGB", (32, 32), color=(10, 80, 40)).save(buf, format="PNG")
    photo = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    dlv_res = rep1.post(f"/api/v1/orders/{ord_res['id']}/deliver", {"pin": pin, "photo": photo}, format="json")
    assert dlv_res.status_code == 200 and dlv_res.json()["data"]["ok"] is True
    assert shop_cli.get("/api/v1/deliveries/locations").json()["data"] == []
