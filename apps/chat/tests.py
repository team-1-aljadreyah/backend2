"""Phase 9 tests: Audit Log, Notifications, Celery operational tasks, and Support Chat."""
from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import PriceTier, Role, SystemSetting, User
from apps.audit.services import record_audit
from apps.common.utils import baghdad_today
from apps.customers.models import Customer, CustomerStatus, ShopMembership
from apps.inventory.models import Batch
from apps.notifications.models import OutboxEvent, OutboxStatus
from apps.notifications.services import enqueue_outbox, notify_role, notify_user
from apps.notifications.tasks import (
    process_outbox_events_task,
    sweep_expired_reservations_task,
    sweep_expiry_alerts_task,
)
from apps.orders.models import Order, OrderStatus
from apps.warehouses.models import Warehouse


@pytest.fixture
def phase9_setup(db):
    wh = Warehouse.objects.create(name="الرئيسي", name_en="Main", code="W1")
    tier = PriceTier.objects.create(name="جملة", name_en="Wholesale", sort_order=1)
    SystemSetting.get_solo()

    mgr1 = User.objects.create_user(phone="07709000001", password="Password123", name="Manager1", role=Role.MANAGER)
    mgr2 = User.objects.create_user(phone="07709000002", password="Password123", name="Manager2", role=Role.MANAGER)
    acct = User.objects.create_user(phone="07709000003", password="Password123", name="Accountant", role=Role.ACCOUNTANT)
    keeper = User.objects.create_user(
        phone="07709000004", password="Password123", name="Keeper", role=Role.WAREHOUSE, warehouse=wh
    )
    staff = User.objects.create_user(
        phone="07709000008", password="Password123", name="Staff", role=Role.STAFF, warehouse=wh
    )
    rep1 = User.objects.create_user(
        phone="07709000005",
        password="Password123",
        name="Rep1",
        role=Role.REP,
        rep_kinds=["sales"],
        rep_categories=["مشروبات"],
    )
    rep2 = User.objects.create_user(
        phone="07709000006",
        password="Password123",
        name="Rep2",
        role=Role.REP,
        rep_kinds=["sales"],
        rep_categories=["مشروبات"],
    )

    cust1 = Customer.objects.create(
        name="أسواق الأمل",
        owner_name="علي",
        phone="07709000007",
        city="بغداد",
        area="الكرادة",
        street="62",
        landmark="الواثق",
        tier=tier,
        rep=rep1,
        debt_limit=500_000,
        status=CustomerStatus.ACTIVE,
        active=True,
    )
    shop_user = User.objects.create_user(phone="07709000007", password="Password123", name="علي", role=Role.SHOP)
    ShopMembership.objects.create(user=shop_user, customer=cust1, is_primary=True, is_active_shop=True)

    return {
        "wh": wh,
        "tier": tier,
        "mgr1": mgr1,
        "mgr2": mgr2,
        "acct": acct,
        "keeper": keeper,
        "staff": staff,
        "rep1": rep1,
        "rep2": rep2,
        "cust1": cust1,
        "shop_user": shop_user,
    }


def _client(phone: str) -> APIClient:
    client = APIClient()
    tok = client.post("/api/v1/auth/login", {"phone": phone, "password": "Password123"}, format="json").json()["data"][
        "token"
    ]
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {tok}")
    return client


@pytest.mark.django_db
def test_audit_log_rbac_normalization_and_filters(phase9_setup):
    mgr = _client("07709000001")
    acct = _client("07709000003")
    keeper = _client("07709000004")
    rep1 = _client("07709000005")
    shop = _client("07709000007")

    record_audit(
        actor=phase9_setup["mgr1"],
        action="item.price_change",
        entity_type="item",
        entity_id=10,
        summary="Updated item price",
        summary_ar="تحديث سعر الصنف",
    )
    record_audit(
        actor=phase9_setup["acct"],
        action="order.approve",
        entity_type="order",
        entity_id=22,
        summary="Approved order",
        summary_ar="الموافقة على الطلب",
    )

    assert keeper.get("/api/v1/audit").status_code == 403
    assert rep1.get("/api/v1/audit").status_code == 403
    assert shop.get("/api/v1/audit").status_code == 403
    assert acct.get("/api/v1/audit").status_code == 200

    res = mgr.get("/api/v1/audit?action=price_change&entity=item")
    assert res.status_code == 200
    rows = res.json()["data"]
    assert any(
        r["action"] == "price_change"
        and r["entity"] == "item"
        and r["summary"] == "تحديث سعر الصنف"
        and r["summary_en"] == "Updated item price"
        for r in rows
    )


@pytest.mark.django_db
def test_notifications_per_user_read_isolation_and_links(phase9_setup):
    mgr1_c = _client("07709000001")
    mgr2_c = _client("07709000002")
    keeper_c = _client("07709000004")

    bcast = notify_role(
        role=Role.MANAGER,
        kind="order_new",
        title_ar="طلب جديد",
        title_en="New order",
        ref_type="order",
        ref_id=55,
    )
    priv = notify_user(
        user_id=phase9_setup["mgr1"].id,
        kind="alert",
        title_ar="تنبيه خاص",
        title_en="Private alert",
        ref_type="invoice",
        ref_id=77,
    )

    list1 = mgr1_c.get("/api/v1/notifications").json()["data"]
    assert len(list1) == 2
    assert next(n for n in list1 if n["id"] == bcast.id)["link"] == "/orders/55"
    assert next(n for n in list1 if n["id"] == priv.id)["link"] == "/invoices/77"
    assert all(n["read"] is False for n in list1)

    # mgr1 marks broadcast read -> mgr2 still sees read=False
    assert mgr1_c.post(f"/api/v1/notifications/{bcast.id}/read", {}, format="json").status_code == 200
    list1_after = mgr1_c.get("/api/v1/notifications").json()["data"]
    assert next(n for n in list1_after if n["id"] == bcast.id)["read"] is True
    assert next(n for n in list1_after if n["id"] == priv.id)["read"] is False

    list2 = mgr2_c.get("/api/v1/notifications").json()["data"]
    assert len(list2) == 1  # only broadcast, not mgr1's private alert
    assert list2[0]["read"] is False

    # keeper cannot mark manager notification read
    assert keeper_c.post(f"/api/v1/notifications/{bcast.id}/read", {}, format="json").status_code == 403

    # read-all clears mgr1's remaining unread notifications
    assert mgr1_c.post("/api/v1/notifications/read-all", {}, format="json").status_code == 200
    list1_all = mgr1_c.get("/api/v1/notifications").json()["data"]
    assert all(n["read"] is True for n in list1_all)
    assert mgr2_c.get("/api/v1/notifications").json()["data"][0]["read"] is False


@pytest.mark.django_db
def test_celery_operational_tasks(phase9_setup):
    mgr = _client("07709000001")
    keeper = _client("07709000004")
    wh = phase9_setup["wh"]
    tier = phase9_setup["tier"]

    item_id = mgr.post(
        "/api/v1/items",
        {
            "name": "عصير",
            "name_en": "Juice",
            "category": "مشروبات",
            "pieces_per_carton": 12,
            "prices": [{"tier_id": tier.id, "carton_price": 12000, "piece_price": 1000}],
        },
        format="json",
    ).json()["data"]["id"]

    near_exp = (baghdad_today() + timedelta(days=5)).isoformat()
    soon_exp = (baghdad_today() + timedelta(days=10)).isoformat()
    valid_exp = (baghdad_today() + timedelta(days=60)).isoformat()

    rec_res = keeper.post(
        "/api/v1/receipts",
        {
            "warehouse_id": wh.id,
            "lines": [
                {"item_id": item_id, "unit": "carton", "qty": 2, "expiry_date": near_exp, "purchase_price": 8000},
                {"item_id": item_id, "unit": "carton", "qty": 1, "expiry_date": soon_exp, "purchase_price": 8000},
                {"item_id": item_id, "unit": "carton", "qty": 5, "expiry_date": valid_exp, "purchase_price": 8000},
            ],
        },
        format="json",
    )
    assert rec_res.status_code == 201
    # Simulate time passing so the 10-day batch becomes expired
    Batch.objects.filter(item_id=item_id, expiry_date=soon_exp).update(
        expiry_date=baghdad_today() - timedelta(days=2)
    )

    # First expiry sweep flags near + expired; second run same day is idempotent
    res1 = sweep_expiry_alerts_task()
    assert res1 == {"near_expiry": 1, "expired": 1}
    assert sweep_expiry_alerts_task() == {"near_expiry": 0, "expired": 0}

    # Shop creates an order (sets temporary reservation), then backdate reserved_until
    shop = _client("07709000007")
    o_res = shop.post(
        "/api/v1/orders",
        {"warehouse_id": wh.id, "lines": [{"item_id": item_id, "unit": "carton", "qty": 1}]},
        format="json",
    )
    assert o_res.status_code == 201
    oid = o_res.json()["data"]["id"]
    Order.objects.filter(id=oid).update(reserved_until=timezone.now() - timedelta(hours=1))
    assert sweep_expired_reservations_task() == 1
    assert Order.objects.get(id=oid).status == OrderStatus.EXPIRED

    # Outbox event processing
    ev = enqueue_outbox("test.topic", {"k": "v"})
    out_res = process_outbox_events_task()
    assert out_res["processed"] >= 1
    ev.refresh_from_db()
    assert ev.status == OutboxStatus.SENT
    assert isinstance(ev, OutboxEvent)


@pytest.mark.django_db
def test_support_chat_rbac_threads_and_read_cursors(phase9_setup):
    mgr = _client("07709000001")
    acct = _client("07709000003")
    keeper = _client("07709000004")
    rep1 = _client("07709000005")
    rep2 = _client("07709000006")
    shop = _client("07709000007")
    staff = _client("07709000008")
    cust_id = phase9_setup["cust1"].id

    # Non-chat company roles (keeper, accountant, reps) are strictly forbidden from chat
    for blocked in (keeper, acct, rep1, rep2):
        assert blocked.get("/api/v1/chat").status_code == 403
        assert blocked.get("/api/v1/chat/me").status_code == 403
        assert blocked.get(f"/api/v1/chat/{cust_id}").status_code == 403
        assert blocked.post(f"/api/v1/chat/{cust_id}", {"text": "test"}, format="json").status_code == 403

    # Validation check on empty text
    assert shop.post("/api/v1/chat/me", {"text": "   "}, format="json").status_code == 400

    # Shop initiates support chat
    send1 = shop.post("/api/v1/chat/me", {"text": "مرحباً، متى يصل الطلب؟"}, format="json")
    assert send1.status_code == 201
    assert send1.json()["data"]["from"] == "shop"
    assert send1.json()["data"]["by"]["role"] == "shop"

    # Manager and staff see waiting=True, unread=1 thread
    mgr_threads = mgr.get("/api/v1/chat").json()["data"]
    assert len(mgr_threads) == 1
    assert mgr_threads[0]["customer_id"] == cust_id
    assert mgr_threads[0]["customer"] == phase9_setup["cust1"].name
    assert mgr_threads[0]["unread"] == 1
    assert mgr_threads[0]["waiting"] is True
    assert "answered_by" not in mgr_threads[0]
    assert len(staff.get("/api/v1/chat").json()["data"]) == 1

    # Staff replies
    reply = staff.post(f"/api/v1/chat/{cust_id}", {"text": "سيصل خلال ساعة"}, format="json")
    assert reply.status_code == 201
    assert reply.json()["data"]["from"] == "staff"
    assert reply.json()["data"]["by"]["role"] == "staff"

    # Thread now shows answered_by and waiting=False
    updated_threads = mgr.get("/api/v1/chat").json()["data"]
    assert updated_threads[0]["waiting"] is False
    assert updated_threads[0]["answered_by"]["name"] == "Staff"

    # Shop sees 2 messages & 1 unread staff reply, then marks read
    me_chat = shop.get("/api/v1/chat/me").json()["data"]
    assert len(me_chat["messages"]) == 2
    assert me_chat["unread"] == 1
    assert shop.post(f"/api/v1/chat/{cust_id}/read", {}, format="json").status_code == 200
    assert shop.get("/api/v1/chat/me").json()["data"]["unread"] == 0

    # Company-side read cursor clears unread across company support team (staff + manager)
    assert mgr.post(f"/api/v1/chat/{cust_id}/read", {}, format="json").status_code == 200
    assert mgr.get("/api/v1/chat").json()["data"][0]["unread"] == 0
    assert staff.get("/api/v1/chat").json()["data"][0]["unread"] == 0
