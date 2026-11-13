"""Comprehensive tests for brand defaults, missing serializer fields, and all 18 new endpoints."""
from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import PriceTier, RepKind, Role, SystemSetting, User
from apps.catalog.models import Item, ItemPrice
from apps.common.utils import baghdad_today
from apps.customers.models import Customer, CustomerStatus, ShopMembership
from apps.inventory.models import Batch
from apps.notifications.models import Notification
from apps.orders.models import Order, OrderLine, OrderStatus
from apps.payments.models import Payment, PaymentMethod
from apps.warehouses.models import Warehouse


def _auth(phone: str, password: str = "Password123") -> APIClient:
    cli = APIClient()
    res = cli.post("/api/v1/auth/login", {"phone": phone, "password": password}, format="json")
    assert res.status_code == 200, res.json()
    tok = res.json()["data"]["token"]
    cli.credentials(HTTP_AUTHORIZATION=f"Bearer {tok}")
    return cli


@pytest.mark.django_db
def test_brand_defaults_and_serializer_fields():
    tier = PriceTier.objects.create(name="جملة", name_en="Wholesale", sort_order=1)
    wh1 = Warehouse.objects.create(name="مخزن 1", name_en="WH1", code="W1", active=True)
    wh2 = Warehouse.objects.create(name="مخزن 2", name_en="WH2", code="W2", active=True)
    mgr = User.objects.create_user(phone="07700000010", password="Password123", name="Manager", role=Role.MANAGER)
    rep = User.objects.create_user(
        phone="07700000011", password="Password123", name="Rep", role=Role.REP, rep_kinds=[RepKind.SALES]
    )
    cust = Customer.objects.create(
        name="محل السلام",
        phone="07700000012",
        city="بغداد",
        area="الكرادة",
        street="62",
        landmark="قرب الساحة",
        tier=tier,
        rep=rep,
        status=CustomerStatus.ACTIVE,
        active=True,
    )
    it = Item.objects.create(name="شاي", name_en="Tea", category="مشروبات", pieces_per_carton=12, active=True)
    ItemPrice.objects.create(item=it, tier=tier, carton_price=24000, piece_price=2000)
    Batch.objects.create(
        item=it,
        warehouse=wh1,
        qty_received=100,
        qty_left=100,
        purchase_price=1500,
        expiry_date=baghdad_today() + timedelta(days=90),
    )

    m_cli = _auth("07700000010")

    # 1. Seed / SiteConfig defaults & summary_hour
    site_res = m_cli.get("/api/v1/site")
    assert site_res.status_code == 200
    site = site_res.json()["data"]
    assert site["brand"]["name"] == "النهرين"
    assert site["brand"]["name_en"] == "Al-Nahrain"
    assert site["brand"]["tagline"] == "للمواد الغذائية بالجملة"
    assert site["brand"]["tagline_en"] == "Wholesale foods"
    assert site["brand"]["company"] == "شركة النهرين للمواد الغذائية"
    assert site["brand"]["company_en"] == "Al-Nahrain Foods Co."
    assert site["look"] == {}

    set_res = m_cli.patch("/api/v1/settings", {"summary_hour": 20}, format="json")
    assert set_res.status_code == 200
    assert set_res.json()["data"]["summary_hour"] == 20

    # 2. User.deleted
    users_res = m_cli.get("/api/v1/users").json()["data"]
    assert all("deleted" in u for u in users_res)

    # 3. Payment.client_ref
    pay_res = m_cli.post(
        "/api/v1/payments",
        {"customer_id": cust.id, "amount": 5000, "method": "cash", "client_ref": "PAY-REF-1"},
        format="json",
    )
    assert pay_res.status_code in (200, 201)
    assert pay_res.json()["data"]["client_ref"] == "PAY-REF-1"

    # 4. Transfer.client_ref, note, diff_reason
    trf_res = m_cli.post(
        "/api/v1/transfers",
        {
            "from_warehouse_id": wh1.id,
            "to_warehouse_id": wh2.id,
            "reason": "نقل",
            "note": "ملاحظة نقل",
            "client_ref": "TRF-REF-1",
            "lines": [{"item_id": it.id, "pieces": 10}],
        },
        format="json",
    )
    assert trf_res.status_code in (200, 201)
    trf = trf_res.json()["data"]
    assert trf["client_ref"] == "TRF-REF-1"
    assert trf["note"] == "ملاحظة نقل"

    recv_res = m_cli.post(
        f"/api/v1/transfers/{trf['id']}/receive",
        {"received": {str(it.id): 8}, "reason": "نقص كرتونين"},
        format="json",
    )
    assert recv_res.status_code == 200
    assert recv_res.json()["data"]["diff_reason"] == "نقص كرتونين"

    # 5. Notification.roles & user_id
    Notification.objects.create(
        user=mgr,
        target_role=Role.MANAGER,
        kind="test",
        title_ar="تنبيه",
        title_en="Alert",
    )
    notifs = m_cli.get("/api/v1/notifications").json()["data"]
    assert len(notifs) >= 1
    assert "roles" in notifs[0]
    assert notifs[0]["user_id"] == mgr.id


@pytest.mark.django_db
def test_visits_custody_alerts_clearances_prices_reorder_and_summary():
    tier = PriceTier.objects.create(name="جملة", name_en="Wholesale", sort_order=1)
    wh = Warehouse.objects.create(name="رئيسي", name_en="Main", code="MW", active=True)
    SystemSetting.get_solo()

    User.objects.create_user(phone="07700000020", password="Password123", name="Mgr", role=Role.MANAGER)
    User.objects.create_user(phone="07700000021", password="Password123", name="Acct", role=Role.ACCOUNTANT)
    rep = User.objects.create_user(
        phone="07700000022", password="Password123", name="Rep", role=Role.REP, rep_kinds=[RepKind.SALES]
    )
    shop_user = User.objects.create_user(phone="07700000023", password="Password123", name="Shop", role=Role.SHOP)
    cust = Customer.objects.create(
        name="سوبرماركت النور",
        phone="07700000023",
        city="بغداد",
        area="المنصور",
        street="14",
        landmark="قرب المول",
        lat=33.315200,
        lng=44.366100,
        tier=tier,
        rep=rep,
        status=CustomerStatus.ACTIVE,
        active=True,
    )
    ShopMembership.objects.create(user=shop_user, customer=cust, is_primary=True, is_active_shop=True)

    it1 = Item.objects.create(name="رز", name_en="Rice", category="حبوب", pieces_per_carton=10, min_stock=50, active=True)
    ItemPrice.objects.create(item=it1, tier=tier, carton_price=20000, piece_price=2000)
    it2 = Item.objects.create(name="سكر", name_en="Sugar", category="حبوب", pieces_per_carton=10, min_stock=5, active=True)
    ItemPrice.objects.create(item=it2, tier=tier, carton_price=10000, piece_price=1000)

    today = baghdad_today()
    b1 = Batch.objects.create(
        item=it1,
        warehouse=wh,
        qty_received=20,
        qty_left=20,
        purchase_price=1500,
        expiry_date=today + timedelta(days=10),
    )

    m_cli = _auth("07700000020")
    a_cli = _auth("07700000021")
    r_cli = _auth("07700000022")
    s_cli = _auth("07700000023")

    # 1. Visits
    js_w = (today.weekday() + 1) % 7
    vd_res = m_cli.post(f"/api/v1/customers/{cust.id}/visit-days", {"days": [js_w]}, format="json")
    assert vd_res.status_code == 200
    assert vd_res.json()["data"]["visit_days"] == [js_w]

    v_res = r_cli.post(
        "/api/v1/visits",
        {
            "customer_id": cust.id,
            "outcome": "no_order",
            "reason": "stocked",
            "note": "عنده بضاعة",
            "lat": 33.325200,
            "lng": 44.366100,
        },
        format="json",
    )
    assert v_res.status_code == 200
    assert v_res.json()["data"]["far"] is True
    assert v_res.json()["data"]["planned"] is True

    assert len(r_cli.get(f"/api/v1/visits?day={today.isoformat()}").json()["data"]) == 1
    rep_v = m_cli.get(f"/api/v1/reports/visits?from={today.isoformat()}&to={today.isoformat()}").json()["data"]
    assert rep_v["totals"]["visited"] == 1
    assert rep_v["totals"]["far"] == 1

    # 2. Cash Custody
    Payment.objects.create(
        number="PAY-C1",
        customer=cust,
        amount=50000,
        unallocated_amount=50000,
        method=PaymentMethod.CASH,
        received_by=rep,
        received_by_name=rep.name,
        received_by_role=rep.role,
    )
    custody_list = a_cli.get("/api/v1/custody").json()["data"]
    assert custody_list[0]["holding"] == 50000

    h_res = r_cli.post("/api/v1/custody/handovers", {"amount": 50000, "note": "تسليم المساء"}, format="json")
    assert h_res.status_code == 200
    hid = h_res.json()["data"]["id"]
    assert h_res.json()["data"]["status"] == "pending"

    c_res = a_cli.post(f"/api/v1/custody/handovers/{hid}/confirm", {"received": 45000}, format="json")
    assert c_res.status_code == 200
    assert c_res.json()["data"]["short"] == 5000

    log = a_cli.get(f"/api/v1/custody/{rep.id}").json()["data"]
    assert log["row"]["holding"] == 5000
    assert log["row"]["short_total"] == 5000

    h2 = r_cli.post("/api/v1/custody/handovers", {"amount": 5000}, format="json").json()["data"]["id"]
    assert r_cli.post(f"/api/v1/custody/handovers/{h2}/cancel").json()["data"]["status"] == "cancelled"

    # 3. Back in Stock
    assert s_cli.post("/api/v1/stock-alerts", {"item_id": it2.id, "on": True}, format="json").json()["data"] == [it2.id]
    assert s_cli.get("/api/v1/stock-alerts").json()["data"] == [it2.id]
    wait = m_cli.get("/api/v1/stock-alerts/waitlist").json()["data"]
    assert len(wait) == 1 and wait[0]["item"]["id"] == it2.id

    rec_res = m_cli.post(
        "/api/v1/receipts",
        {
            "warehouse_id": wh.id,
            "supplier": "مورد 1",
            "lines": [{"item_id": it2.id, "unit": "carton", "qty": 2, "purchase_price": 8000, "expiry_date": (today + timedelta(days=120)).isoformat()}],
        },
        format="json",
    )
    assert rec_res.status_code in (200, 201)
    assert s_cli.get("/api/v1/stock-alerts").json()["data"] == []

    # 4. Clearances
    cl_res = m_cli.post("/api/v1/clearances", {"batch_id": b1.id, "percent": 20}, format="json")
    assert cl_res.status_code == 200
    cl_id = cl_res.json()["data"]["id"]
    items_list = s_cli.get("/api/v1/items").json()["data"]
    it1_data = next(x for x in items_list if x["id"] == it1.id)
    assert it1_data["clearance"]["percent"] == 20
    assert m_cli.post(f"/api/v1/clearances/{cl_id}/cancel").json()["data"]["status"] == "cancelled"

    # 5. Dated Prices
    future_day = (today + timedelta(days=3)).isoformat()
    sp_res = m_cli.post(
        "/api/v1/price-schedule",
        {"item_ids": [it1.id], "start": future_day, "percent": 10, "note": "زيادة قادمة"},
        format="json",
    )
    assert sp_res.status_code == 200
    sp_id = sp_res.json()["data"]["id"]
    hist = m_cli.get(f"/api/v1/items/{it1.id}/price-history").json()["data"]
    assert len(hist["scheduled"]) == 1
    assert m_cli.post(f"/api/v1/price-schedule/{sp_id}/cancel").json()["data"]["status"] == "cancelled"

    # 6. Reorder Reminders (create 2 historical orders 5 days apart)
    o1 = Order.objects.create(
        number="ORD-R1",
        customer=cust,
        warehouse=wh,
        created_by=shop_user,
        status=OrderStatus.CONFIRMED,
        subtotal=20000,
        total=20000,
    )
    Order.objects.filter(id=o1.id).update(created_at=timezone.now() - timedelta(days=10))
    OrderLine.objects.create(order=o1, item=it1, warehouse=wh, unit="carton", qty=1, pieces=10, unit_price=20000, total=20000)

    o2 = Order.objects.create(
        number="ORD-R2",
        customer=cust,
        warehouse=wh,
        created_by=shop_user,
        status=OrderStatus.CONFIRMED,
        subtotal=20000,
        total=20000,
    )
    Order.objects.filter(id=o2.id).update(created_at=timezone.now() - timedelta(days=6))
    OrderLine.objects.create(order=o2, item=it1, warehouse=wh, unit="carton", qty=1, pieces=10, unit_price=20000, total=20000)

    due = s_cli.get("/api/v1/reorder/due").json()["data"]
    assert len(due) == 1 and due[0]["item_id"] == it1.id
    late = r_cli.get("/api/v1/reorder/late-shops").json()["data"]
    assert len(late) == 1 and late[0]["customer"]["id"] == cust.id

    # 7. Day Summary
    ds = m_cli.get(f"/api/v1/reports/day-summary?date={today.isoformat()}").json()["data"]
    assert ds["date"] == today.isoformat()
    assert ds["hour"] == SystemSetting.get_solo().summary_hour
    assert len(ds["low"]) >= 1
