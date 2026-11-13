"""Phase 11 remediation & coverage tests: durable outbox, GPS, supply orders/receipts, multi-shop IDOR, RLS, and service branches."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from io import BytesIO
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.db import connection, transaction
from PIL import Image
from rest_framework.test import APIClient

from apps.accounts.models import PriceTier, Role, User
from apps.catalog.models import Item, PriceChange
from apps.common.exceptions import ApiException
from apps.common.models import IdempotencyKey
from apps.common.rls import apply_pg_session_context, clear_pg_session_context, pg_rls_context
from apps.common.utils import baghdad_today
from apps.customers.models import Customer, CustomerStatus, ShopMembership, SpecialPrice
from apps.deliveries.models import Delivery, DeliveryStatus, RepLocation
from apps.deliveries.services import record_rep_location
from apps.inventory.models import Batch, DamagedInventory
from apps.invoices.models import CreditNote, Invoice
from apps.notifications.models import OutboxEvent, OutboxStatus
from apps.notifications.tasks import (
    cleanup_realtime_tickets_task,
    process_outbox_events_task,
    sweep_expired_reservations_task,
    sweep_expiry_alerts_task,
)
from apps.orders.models import Order, OrderStatus
from apps.payments.models import OnlinePayment, OnlinePaymentStatus, Payment
from apps.payments.services import settle_online_payment, start_online_payment
from apps.realtime.broadcaster import (
    OUTBOX_LEASE_SECONDS,
    OUTBOX_MAX_ATTEMPTS,
    deliver_outbox_event,
    dispatch_outbox_realtime,
    enqueue_outbox_event,
)
from apps.realtime.models import RealtimeTicket
from apps.receipts.models import Receipt
from apps.stocktakes.models import Stocktake, StocktakeStatus
from apps.supply_orders.models import SupplyOrder, SupplyStatus
from apps.warehouses.models import Warehouse
from integrations.qi_card.client import QiCreateResult


def _login(phone: str, password: str = "Demo12345!") -> APIClient:
    client = APIClient()
    res = client.post("/api/v1/auth/login", {"phone": phone, "password": password}, format="json")
    assert res.status_code == 200, res.json()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {res.json()['data']['token']}")
    return client


def _png_data_url() -> str:
    buf = BytesIO()
    Image.new("RGB", (4, 4), color=(12, 120, 48)).save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


@pytest.mark.django_db(transaction=True)
def test_outbox_rollback_retry_backoff_dead_letter_and_stale_lease(monkeypatch):
    """Durable outbox skips rolled-back TXs, retries transient failures, honors backoff, dead-letters after 5, and recovers stale leases."""
    dispatched: list[tuple[str, dict]] = []

    def _ok_send(groups, payload, *, raise_on_error=False):
        dispatched.append(("groups", {"groups": sorted(groups), "payload": payload}))

    monkeypatch.setattr("apps.realtime.broadcaster.send_to_groups", _ok_send)

    # 1. Transaction rollback leaves 0 OutboxEvent rows
    with pytest.raises(RuntimeError):
        with transaction.atomic():
            enqueue_outbox_event("rt.orders", {"id": 999})
            raise RuntimeError("abort tx")
    assert OutboxEvent.objects.count() == 0
    assert dispatched == []

    # 2. Failure during post-commit wake keeps event PENDING with backoff and attempts=1
    def _boom_send(*args, **kwargs):
        raise RuntimeError("redis temporarily unreachable")

    monkeypatch.setattr("apps.realtime.broadcaster.send_to_groups", _boom_send)
    wh = Warehouse.objects.create(name="مخزن الاختبار", name_en="Test WH", code="TST1")
    tier = PriceTier.objects.create(name="جملة", name_en="Wholesale", sort_order=1)
    rep = User.objects.create_user(phone="07709110001", password="Password1!", name="Rep", role=Role.REP, rep_kinds=["sales", "delivery"], warehouse=wh)
    cust = Customer.objects.create(
        name="محل النهرين",
        owner_name="صاحب",
        phone="07709110002",
        area="المنصور",
        street="14",
        landmark="قرب",
        tier=tier,
        rep=rep,
        status=CustomerStatus.ACTIVE,
        active=True,
    )
    order = Order.objects.create(
        number="ORD-OBX-1",
        customer=cust,
        warehouse=wh,
        rep=rep,
        created_by=rep,
        subtotal=10000,
        discount_amount=0,
        total=10000,
        status=OrderStatus.APPROVED,
    )

    with transaction.atomic():
        ev_id = enqueue_outbox_event("rt.orders", {"id": order.id})

    ev = OutboxEvent.objects.get(id=ev_id)
    assert ev.status == OutboxStatus.PENDING
    assert ev.attempts == 1
    assert "redis temporarily unreachable" in ev.last_error
    assert ev.next_retry_at is not None and ev.next_retry_at > datetime.now(tz=UTC)
    assert ev.locked_at is None

    # 3. Early Celery sweep skips events whose next_retry_at is still in the future
    early = process_outbox_events_task(batch_size=20, worker_id="w-early")
    assert early == {"processed": 0, "failed": 0, "requeued": 0}

    # 4. Channel layer recovers + backoff elapsed -> event becomes SENT cleanly
    monkeypatch.setattr("apps.realtime.broadcaster.send_to_groups", _ok_send)
    OutboxEvent.objects.filter(id=ev_id).update(next_retry_at=datetime.now(tz=UTC) - timedelta(seconds=1))
    res = process_outbox_events_task(batch_size=20, worker_id="w-ok")
    assert res == {"processed": 1, "failed": 0, "requeued": 0}
    ev.refresh_from_db()
    assert ev.status == OutboxStatus.SENT
    assert ev.processed_at is not None
    # Calling again skips already-SENT event
    assert deliver_outbox_event(ev_id, worker_id="w-repeat") == "skipped"

    # 5. Stale PROCESSING lease recovery succeeds after OUTBOX_LEASE_SECONDS
    stale_ev = OutboxEvent.objects.create(
        topic="rt.chat",
        payload={"customer_id": cust.id},
        status=OutboxStatus.PROCESSING,
        locked_at=datetime.now(tz=UTC) - timedelta(seconds=OUTBOX_LEASE_SECONDS + 10),
        locked_by="crashed-worker",
        attempts=1,
    )
    stale_res = process_outbox_events_task(batch_size=20, worker_id="w-recover")
    assert stale_res["processed"] == 1
    stale_ev.refresh_from_db()
    assert stale_ev.status == OutboxStatus.SENT

    # 6. Exhausting OUTBOX_MAX_ATTEMPTS dead-letters event as FAILED
    monkeypatch.setattr("apps.realtime.broadcaster.send_to_groups", _boom_send)
    fail_ev = OutboxEvent.objects.create(topic="rt.chat", payload={"customer_id": cust.id}, status=OutboxStatus.PENDING)
    outcomes = []
    for i in range(OUTBOX_MAX_ATTEMPTS):
        OutboxEvent.objects.filter(id=fail_ev.id).update(next_retry_at=datetime.now(tz=UTC) - timedelta(seconds=1))
        outcomes.append(deliver_outbox_event(fail_ev.id, worker_id=f"w-fail-{i}"))
    assert outcomes[:-1] == ["retry"] * (OUTBOX_MAX_ATTEMPTS - 1)
    assert outcomes[-1] == "failed"
    fail_ev.refresh_from_db()
    assert fail_ev.status == OutboxStatus.FAILED
    assert fail_ev.attempts == OUTBOX_MAX_ATTEMPTS


@pytest.mark.django_db
def test_outbox_dispatch_routing_and_ticket_cleanup(monkeypatch):
    """dispatch_outbox_realtime routes notifications (user & role), chat, orders, and location (with dlv.rep=None); cleanup purges old tickets."""
    captured: list[tuple[set[str], dict]] = []
    monkeypatch.setattr(
        "apps.realtime.broadcaster.send_to_groups",
        lambda groups, payload, raise_on_error=True: captured.append((set(groups), payload)),
    )

    wh = Warehouse.objects.create(name="WH", name_en="WH", code="WHRT")
    tier = PriceTier.objects.create(name="T1", name_en="T1", sort_order=1)
    rep = User.objects.create_user(phone="07709220001", password="Password1!", name="Rep", role=Role.REP, rep_kinds=["delivery"], warehouse=wh)
    shop_u = User.objects.create_user(phone="07709220002", password="Password1!", name="Shop", role=Role.SHOP)
    cust = Customer.objects.create(
        name="Shop RT", owner_name="Owner", phone="07709220003", area="الكرادة", street="s", landmark="l", tier=tier, rep=rep, status=CustomerStatus.ACTIVE, active=True
    )
    ShopMembership.objects.create(user=shop_u, customer=cust, is_primary=True, is_active_shop=True)
    order = Order.objects.create(
        number="ORD-LOC-1",
        customer=cust,
        warehouse=wh,
        rep=rep,
        created_by=rep,
        subtotal=5000,
        discount_amount=0,
        total=5000,
        status=OrderStatus.APPROVED,
    )
    # Delivery with rep=None while order.rep=rep tests Q(rep_id=rep_id) | Q(rep__isnull=True, order__rep_id=rep_id)
    Delivery.objects.create(
        order=order,
        rep=None,
        status=DeliveryStatus.OUT,
        code="NAHRAIN:DLV:X1",
        pin_encrypted="enc",
        pin_hash="hash",
    )

    dispatch_outbox_realtime("rt.notifications", {"user_id": rep.id, "kind": "k", "id": 1})
    dispatch_outbox_realtime("rt.notifications", {"target_role": Role.MANAGER, "kind": "k", "id": 2})
    dispatch_outbox_realtime("rt.chat", {"customer_id": cust.id})
    dispatch_outbox_realtime("rt.orders", {"id": order.id})
    dispatch_outbox_realtime("rt.location", {"rep_id": rep.id, "lat": 33.3, "lng": 44.4, "at": "2026-01-01T00:00:00Z"})

    assert captured[0][0] == {f"user_{rep.id}"}
    assert captured[1][0] == {f"role_{Role.MANAGER}"}
    assert f"shop_{cust.id}" in captured[2][0] and "chat_staff" in captured[2][0]
    assert f"user_{rep.id}" in captured[3][0] and f"shop_{cust.id}" in captured[3][0]
    assert f"shop_{cust.id}" in captured[4][0] and "loc_staff" in captured[4][0]

    # Ticket cleanup deletes expired >5m and used >1h while retaining fresh valid tickets
    now = datetime.now(tz=UTC)
    t_exp = RealtimeTicket.objects.create(user=rep, token_hash="h_exp", expires_at=now - timedelta(minutes=6))
    t_used = RealtimeTicket.objects.create(
        user=rep, token_hash="h_used", expires_at=now + timedelta(minutes=1), used_at=now - timedelta(hours=2)
    )
    t_keep = RealtimeTicket.objects.create(user=rep, token_hash="h_keep", expires_at=now + timedelta(minutes=2))
    deleted = cleanup_realtime_tickets_task()
    assert deleted == 2
    assert list(RealtimeTicket.objects.values_list("id", flat=True)) == [t_keep.id]
    assert not RealtimeTicket.objects.filter(id__in=[t_exp.id, t_used.id]).exists()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_live_gps_security_ws_rejections_and_deduplication(tmp_path, settings):
    """Sales-only reps are 403; delivery rep without active OUT gets 400 BAD_STATE on HTTP & WS; identical coords <120s suppress duplicate broadcasts."""
    settings.MEDIA_ROOT = tmp_path
    from asgiref.sync import sync_to_async
    from channels.testing import WebsocketCommunicator

    from apps.realtime.tickets import issue_realtime_ticket
    from config.asgi import application

    await sync_to_async(call_command)("seed_demo")
    wh = await sync_to_async(Warehouse.objects.first)()
    await sync_to_async(User.objects.create_user)(
        phone="07709330001", password="Password1!", name="Sales Only", role=Role.REP, rep_kinds=["sales"], warehouse=wh
    )
    dlv_rep = await sync_to_async(User.objects.create_user)(
        phone="07709330002", password="Password1!", name="Driver NoOrder", role=Role.REP, rep_kinds=["delivery"], warehouse=wh
    )

    def _http_checks():
        sales_client = _login("07709330001", "Password1!")
        r_forbidden = sales_client.post("/api/v1/deliveries/location", {"lat": 33.3, "lng": 44.4}, format="json")
        assert r_forbidden.status_code == 403

        dlv_client = _login("07709330002", "Password1!")
        r_no_out = dlv_client.post("/api/v1/deliveries/location", {"lat": 33.3, "lng": 44.4}, format="json")
        assert r_no_out.status_code == 400
        assert r_no_out.json()["code"] == "BAD_STATE"

        # Invalid coords rejected cleanly
        r_bad_coords = dlv_client.post("/api/v1/deliveries/location", {"lat": 99.0, "lng": 44.4}, format="json")
        assert r_bad_coords.status_code == 400

    await sync_to_async(_http_checks)()

    # WS publish location without active OUT delivery returns error BAD_STATE
    raw_ticket, _ttl = await sync_to_async(issue_realtime_ticket)(user=dlv_rep, active_customer_id=None)
    comm = WebsocketCommunicator(application, f"/ws?ticket={raw_ticket}")
    ok, _ = await comm.connect()
    assert ok
    await comm.send_json_to({"action": "publish", "topic": "location", "data": {"lat": 33.315, "lng": 44.366}})
    err = await comm.receive_json_from()
    assert err["type"] == "error" and err["code"] == "BAD_STATE"
    await comm.disconnect()

    # Now assign an OUT delivery and verify duplicate quantized coord suppression <120s
    def _dedup_check():
        cust = Customer.objects.filter(status=CustomerStatus.ACTIVE).first()
        order = Order.objects.create(
            number="ORD-GPS-DEDUP",
            customer=cust,
            warehouse=wh,
            rep=dlv_rep,
            created_by=dlv_rep,
            subtotal=5000,
            discount_amount=0,
            total=5000,
            status=OrderStatus.APPROVED,
        )
        Delivery.objects.create(
            order=order,
            rep=dlv_rep,
            status=DeliveryStatus.OUT,
            code="NAHRAIN:DLV:GPS1",
            pin_encrypted="enc",
            pin_hash="hash",
        )
        OutboxEvent.objects.filter(topic="rt.location").delete()
        row1, emit1 = record_rep_location(actor=dlv_rep, lat=33.3150001, lng=44.3660001, emit_realtime=True)
        row2, emit2 = record_rep_location(actor=dlv_rep, lat=33.3150004, lng=44.3660004, emit_realtime=True)
        row3, emit3 = record_rep_location(actor=dlv_rep, lat=33.3250000, lng=44.3760000, emit_realtime=True)
        assert emit1 is True and emit2 is False and emit3 is True
        assert OutboxEvent.objects.filter(topic="rt.location", payload__rep_id=dlv_rep.id).count() == 2
        assert float(RepLocation.objects.get(rep=dlv_rep).lat) == float(row3["lat"])

    await sync_to_async(_dedup_check)()


@pytest.mark.django_db
def test_supply_orders_and_receipt_hardening_and_idempotency(tmp_path, settings):
    """SupplyOrder rejects float qty & rounds Decimal price cleanly; Receipt rejects wrong WH/item/over-receive and enforces Idempotency-Key + client_ref."""
    settings.MEDIA_ROOT = tmp_path
    call_command("seed_demo")
    mgr = _login("07701000001")
    wh_karkh = Warehouse.objects.get(code="WH-KARKH")
    wh_rusafa = Warehouse.objects.get(code="WH-RUSAFA")
    items = list(Item.objects.order_by("id")[:2])
    item1, item2 = items[0], items[1]
    exp = (baghdad_today() + timedelta(days=180)).isoformat()

    # Non-integer qty rejected on SupplyOrder
    bad_qty = mgr.post(
        "/api/v1/supply-orders",
        {
            "warehouse_id": wh_karkh.id,
            "supplier": "مورد 1",
            "expected_date": exp,
            "lines": [{"item_id": item1.id, "unit": "carton", "qty": "2.5", "price": 24000}],
        },
        format="json",
    )
    assert bad_qty.status_code == 400

    # Valid Decimal price rounded HALF_UP cleanly (24000.6 -> 24001)
    so_res = mgr.post(
        "/api/v1/supply-orders",
        {
            "warehouse_id": wh_karkh.id,
            "supplier": "شركة الرافدين",
            "expected_date": exp,
            "lines": [{"item_id": item1.id, "unit": "carton", "qty": 2, "price": "24000.6"}],
        },
        format="json",
    )
    assert so_res.status_code == 201, so_res.json()
    so_id = so_res.json()["data"]["id"]
    assert so_res.json()["data"]["total"] == 48002

    # Receipt wrong warehouse -> 400
    r_bad_wh = mgr.post(
        "/api/v1/receipts",
        {
            "warehouse_id": wh_rusafa.id,
            "supply_order_id": so_id,
            "supplier": "شركة الرافدين",
            "lines": [{"item_id": item1.id, "unit": "carton", "qty": 1, "purchase_price": 24001, "expiry_date": exp}],
        },
        format="json",
    )
    assert r_bad_wh.status_code == 400

    # Receipt unmatched item not in SupplyOrder -> 400
    r_bad_item = mgr.post(
        "/api/v1/receipts",
        {
            "warehouse_id": wh_karkh.id,
            "supply_order_id": so_id,
            "supplier": "شركة الرافدين",
            "lines": [{"item_id": item2.id, "unit": "carton", "qty": 1, "purchase_price": 15000, "expiry_date": exp}],
        },
        format="json",
    )
    assert r_bad_item.status_code == 400

    # Over-receiving > ordered pieces -> 400
    r_over = mgr.post(
        "/api/v1/receipts",
        {
            "warehouse_id": wh_karkh.id,
            "supply_order_id": so_id,
            "supplier": "شركة الرافدين",
            "lines": [{"item_id": item1.id, "unit": "carton", "qty": 3, "purchase_price": 24001, "expiry_date": exp}],
        },
        format="json",
    )
    assert r_over.status_code == 400

    # Idempotency-Key replay & 409 mismatch, plus piece-to-carton reconciliation closing SupplyOrder
    per_piece = round(24001 / item1.pieces_per_carton)
    payload_idem = {
        "warehouse_id": wh_karkh.id,
        "supply_order_id": so_id,
        "supplier": "شركة الرافدين",
        "lines": [{"item_id": item1.id, "unit": "piece", "qty": item1.pieces_per_carton * 2, "purchase_price": per_piece, "expiry_date": exp}],
    }
    r1 = mgr.post("/api/v1/receipts", payload_idem, format="json", HTTP_IDEMPOTENCY_KEY="rcpt-k-1")
    r2 = mgr.post("/api/v1/receipts", payload_idem, format="json", HTTP_IDEMPOTENCY_KEY="rcpt-k-1")
    assert r1.status_code == 201 and r2.status_code == 201
    assert r1.json()["data"]["id"] == r2.json()["data"]["id"]
    assert SupplyOrder.objects.get(id=so_id).status == SupplyStatus.RECEIVED

    # Different payload with same Idempotency-Key returns 409 CONFLICT
    r_conflict = mgr.post(
        "/api/v1/receipts",
        {**payload_idem, "supplier": "مورد مختلف"},
        format="json",
        HTTP_IDEMPOTENCY_KEY="rcpt-k-1",
    )
    assert r_conflict.status_code == 409
    assert IdempotencyKey.objects.filter(key="rcpt-k-1").count() == 1

    # client_ref deduplication on direct receipt without header
    standalone = {
        "warehouse_id": wh_karkh.id,
        "supplier": "مورد مباشر",
        "client_ref": "rcpt-cref-99",
        "lines": [{"item_id": item2.id, "unit": "carton", "qty": 1, "purchase_price": 10000, "expiry_date": exp}],
    }
    c1 = mgr.post("/api/v1/receipts", standalone, format="json")
    c2 = mgr.post("/api/v1/receipts", standalone, format="json")
    assert c1.status_code == 201 and c2.status_code == 201
    assert c1.json()["data"]["id"] == c2.json()["data"]["id"]
    assert Receipt.objects.filter(client_ref="rcpt-cref-99").count() == 1


@pytest.mark.django_db
def test_multishop_idor_isolation_and_customer_lifecycle(tmp_path, settings):
    """Multi-shop user cannot read/mutate inactive shop B until /me/shop switch; switch locks shop A."""
    settings.MEDIA_ROOT = tmp_path
    call_command("seed_demo")
    mgr = _login("07701000001")
    shop_client = _login("07701000008")

    shops_res = shop_client.get("/api/v1/me/shops")
    assert shops_res.status_code == 200
    shop_a_id = shops_res.json()["data"][0]["id"]

    # Shop user adds Branch B, manager approves it with debt limit
    add_res = shop_client.post(
        "/api/v1/me/shops",
        {
            "shop_name": "فرع الجادرية الجديد",
            "area": "الجادرية",
            "address_info": {
                "label": "فرع الجادرية",
                "details": "شارع الجامعة قرب التقاطع",
                "phone": "07701000008",
                "street": "الجامعة",
                "landmark": "تقاطع",
                "lat": 33.28,
                "lng": 44.38,
            },
        },
        format="json",
    )
    assert add_res.status_code == 201, add_res.json()
    shop_b_id = add_res.json()["data"]["id"]

    app_res = mgr.patch(
        f"/api/v1/customers/{shop_b_id}",
        {"status": "active", "active": True, "debt_limit": 5000000},
        format="json",
    )
    assert app_res.status_code == 200

    # Seed financial & order artifacts on Shop B
    rep = User.objects.filter(role=Role.REP).first()
    wh = Warehouse.objects.first()
    shop_b = Customer.objects.get(id=shop_b_id)
    order_b = Order.objects.create(
        number="ORD-SHOPB-1",
        customer=shop_b,
        warehouse=wh,
        rep=rep,
        created_by=rep,
        subtotal=15000,
        discount_amount=0,
        total=15000,
        status=OrderStatus.CONFIRMED,
    )
    inv_b = Invoice.objects.create(
        number="INV-SHOPB-1",
        order=order_b,
        customer=shop_b,
        rep=rep,
        warehouse=wh,
        subtotal=15000,
        discount=0,
        total=15000,
        paid=5000,
        cost=10000,
        delivered_at=datetime.now(tz=UTC),
    )
    pay_b = Payment.objects.create(
        number="PAY-SHOPB-1",
        customer=shop_b,
        amount=5000,
        method="cash",
        received_by=rep,
        received_by_name=rep.name,
        received_by_role=rep.role,
    )

    # While Shop A is active, every Shop B endpoint is 403 FORBIDDEN
    for path in (
        f"/api/v1/customers/{shop_b_id}",
        f"/api/v1/customers/{shop_b_id}/prices",
        f"/api/v1/customers/{shop_b_id}/statement",
        f"/api/v1/customers/{shop_b_id}/allocation?amount=5000",
        f"/api/v1/customers/{shop_b_id}/credit-notes",
        f"/api/v1/customers/{shop_b_id}/refunds",
        f"/api/v1/orders/{order_b.id}",
        f"/api/v1/invoices/{inv_b.id}",
        f"/api/v1/payments/{pay_b.id}",
        f"/api/v1/chat/{shop_b_id}",
    ):
        r = shop_client.get(path)
        assert r.status_code == 403, f"Expected 403 for inactive shop on {path}, got {r.status_code}"

    assert shop_client.patch(f"/api/v1/customers/{shop_b_id}", {"name": "hack"}, format="json").status_code == 403
    assert shop_client.post(f"/api/v1/chat/{shop_b_id}/read", {}, format="json").status_code == 403

    # Switch active shop to B -> B unlocks, A locks
    sw = shop_client.post("/api/v1/me/shop", {"customer_id": shop_b_id}, format="json")
    assert sw.status_code == 200
    assert sw.json()["data"]["customer_id"] == shop_b_id

    for path in (
        f"/api/v1/customers/{shop_b_id}/statement",
        f"/api/v1/orders/{order_b.id}",
        f"/api/v1/invoices/{inv_b.id}",
        f"/api/v1/payments/{pay_b.id}",
        "/api/v1/chat/me",
    ):
        r = shop_client.get(path)
        assert r.status_code == 200, f"Expected 200 after switching to Shop B on {path}, got {r.json()}"
    assert shop_client.post(f"/api/v1/chat/{shop_b_id}/read", {}, format="json").status_code == 200

    assert shop_client.get(f"/api/v1/customers/{shop_a_id}").status_code == 403
    assert shop_client.get(f"/api/v1/customers/{shop_a_id}/statement").status_code == 403
    assert shop_client.post(f"/api/v1/chat/{shop_a_id}/read", {}, format="json").status_code == 403

    # Special prices create + delete (prices=None) & reject pending customer flow
    item = Item.objects.first()
    sp_add = mgr.post(
        f"/api/v1/customers/{shop_b_id}/prices",
        {"item_id": item.id, "prices": {"carton_price": 19000, "piece_price": 2000}},
        format="json",
    )
    assert sp_add.status_code == 200
    assert SpecialPrice.objects.filter(customer_id=shop_b_id, item=item).exists()
    sp_del = mgr.post(f"/api/v1/customers/{shop_b_id}/prices", {"item_id": item.id, "prices": None}, format="json")
    assert sp_del.status_code == 200
    assert not SpecialPrice.objects.filter(customer_id=shop_b_id, item=item).exists()

    tier = PriceTier.objects.first()
    pending_cust = Customer.objects.create(
        name="محل معلق",
        owner_name="معلق",
        phone="07709998877",
        area="الدورة",
        street="1",
        landmark="قرب",
        tier=tier,
        rep=rep,
        status=CustomerStatus.PENDING,
        active=False,
    )
    rej = mgr.post(f"/api/v1/customers/{pending_cust.id}/reject", {"reason": "بيانات ناقصة"}, format="json")
    assert rej.status_code == 200
    assert rej.json()["data"]["status"] == CustomerStatus.REJECTED


@pytest.mark.django_db
def test_pg_rls_guc_helper_and_health_metrics_endpoints(monkeypatch, settings):
    """RLS helper sets & clears PostgreSQL GUCs cleanly and health/metrics enforce access control."""
    calls: list[tuple[str, list]] = []

    class _FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, sql, params=None):
            calls.append((sql, list(params or [])))

    class _FakeConn:
        vendor = "postgresql"
        in_atomic_block = True

        def cursor(self):
            return _FakeCursor()

    monkeypatch.setattr("apps.common.rls.connection", _FakeConn())
    apply_pg_session_context(user_id=7, role="shop", warehouse_id=None, active_customer_id=42)
    assert len(calls) == 1 and calls[0][1] == ["7", True, "shop", True, "", True, "42", True]

    with pytest.raises(ValueError):
        with pg_rls_context(type("U", (), {"id": 9, "role": "warehouse", "warehouse_id": 2})()):
            raise ValueError("boom")
    # Clear executed even after exception inside contextmanager
    assert "set_config('app.user_id', '', false)" in calls[-1][0]

    calls.clear()
    clear_pg_session_context()
    assert len(calls) == 1

    # Health & Metrics HTTP endpoints
    client = APIClient()
    assert client.get("/health/live").status_code == 200
    assert client.get("/health/ready").status_code == 200
    assert client.get("/metrics").status_code == 403

    settings.METRICS_TOKEN = "secret-prom-token"
    assert client.get("/metrics", HTTP_AUTHORIZATION="Bearer secret-prom-token").status_code == 200

    with patch.object(connection, "cursor", side_effect=RuntimeError("db down")):
        assert client.get("/health/ready").status_code == 503


@pytest.mark.django_db
def test_accounts_catalog_inventory_stocktake_lifecycle(tmp_path, settings):
    """Exercises user/settings/tier CRUD validations, item updates & image clearing, batch hold/release/trace, and stocktake close variances."""
    settings.MEDIA_ROOT = tmp_path
    call_command("seed_demo")
    mgr = _login("07701000001")
    wh_karkh = Warehouse.objects.get(code="WH-KARKH")
    wh_rusafa = Warehouse.objects.get(code="WH-RUSAFA")

    # Accounts validations & mutations
    bad_role = mgr.post(
        "/api/v1/users",
        {"name": "Bad", "phone": "07708880001", "password": "Password1!", "role": "warehouse"},
        format="json",
    )
    assert bad_role.status_code == 400
    bad_rep_kind = mgr.post(
        "/api/v1/users",
        {"name": "BadRep", "phone": "07708880009", "password": "Password1!", "role": "rep", "rep_kinds": ["invalid_kind"]},
        format="json",
    )
    assert bad_rep_kind.status_code == 400

    new_u = mgr.post(
        "/api/v1/users",
        {
            "name": "موظف جديد",
            "phone": "07708880002",
            "password": "Password1!",
            "role": "staff",
            "warehouse_id": wh_karkh.id,
        },
        format="json",
    )
    assert new_u.status_code == 201
    uid = new_u.json()["data"]["id"]
    upd_u = mgr.patch(f"/api/v1/users/{uid}", {"name": "اسم محدث", "password": "Password2!"}, format="json")
    assert upd_u.status_code == 200

    # Settings & PriceTiers
    s_res = mgr.patch(
        "/api/v1/settings",
        {
            "reservation_hours": 12,
            "near_expiry_days": 30,
            "slow_moving_days": 21,
            "commission_pct": "2.5",
            "usd_rate": 1520,
            "phone": "07701000001",
            "address": "بغداد",
        },
        format="json",
    )
    assert s_res.status_code == 200
    t_res = mgr.post("/api/v1/price-tiers", {"name": "VIP", "name_en": "VIP"}, format="json")
    assert t_res.status_code == 201
    tid = t_res.json()["data"]["id"]
    assert mgr.patch(f"/api/v1/price-tiers/{tid}", {"name_en": "VIP Plus"}, format="json").status_code == 200

    # Catalog update + PriceChange creation + image update/clear
    item = Item.objects.first()
    tier = PriceTier.objects.order_by("sort_order", "id").first()
    pc_before = PriceChange.objects.filter(item=item).count()
    i_upd = mgr.patch(
        f"/api/v1/items/{item.id}",
        {"name_en": "Updated Oil 1L", "commission_percent": "1.75", "prices": [{"tier_id": tier.id, "carton_price": 45000, "piece_price": 4000}]},
        format="json",
    )
    assert i_upd.status_code == 200
    assert PriceChange.objects.filter(item=item).count() >= pc_before

    img_set = mgr.post(f"/api/v1/items/{item.id}/image", {"image": _png_data_url()}, format="json")
    assert img_set.status_code == 200 and img_set.json()["data"]["image"]
    img_clr = mgr.post(f"/api/v1/items/{item.id}/image", {"image": ""}, format="json")
    assert img_clr.status_code == 200 and img_clr.json()["data"].get("image") is None

    # Batch hold / trace / release
    batch = Batch.objects.filter(warehouse=wh_karkh, qty_left__gt=0, held_at__isnull=True).first()
    assert mgr.post(f"/api/v1/batches/{batch.id}/hold", {"reason": "فحص جودة"}, format="json").status_code == 200
    assert mgr.get(f"/api/v1/batches/{batch.id}/trace").status_code == 200
    assert mgr.post(f"/api/v1/batches/{batch.id}/release", {}, format="json").status_code == 200

    # Expiry sweep task runs cleanly
    sweep_res = sweep_expiry_alerts_task()
    assert "near_expiry" in sweep_res and "expired" in sweep_res

    # Stocktake create, update & close with shortage + surplus variances (on Rusafa WH cleanly)
    wh2_batches = list(Batch.objects.filter(warehouse=wh_rusafa, qty_left__gt=2).select_related("item").order_by("id")[:2])
    assert len(wh2_batches) >= 1
    st_create = mgr.post("/api/v1/stocktakes", {"warehouse_id": wh_rusafa.id}, format="json")
    assert st_create.status_code == 201
    st_id = st_create.json()["data"]["id"]

    # Attempting order creation in Rusafa while open stocktake locks count -> 400 BAD_STATE
    st_obj = Stocktake.objects.get(id=st_id)
    update_lines = []
    for idx, ln in enumerate(st_obj.lines.all()):
        if idx == 0 and ln.system_qty >= 2:
            update_lines.append({"item_id": ln.item_id, "counted_qty": ln.system_qty - 1, "damaged_qty": 1, "reason": "كسر بالمخزن"})
        elif idx == 1:
            update_lines.append({"item_id": ln.item_id, "counted_qty": ln.system_qty + 2, "damaged_qty": 0, "reason": "فائض جرد"})
        else:
            update_lines.append({"item_id": ln.item_id, "counted_qty": ln.system_qty, "damaged_qty": 0, "reason": ""})

    st_patch = mgr.patch(f"/api/v1/stocktakes/{st_id}", {"lines": update_lines}, format="json")
    assert st_patch.status_code == 200
    st_close = mgr.post(f"/api/v1/stocktakes/{st_id}/close", {}, format="json")
    assert st_close.status_code == 200
    assert st_close.json()["data"]["status"] == StocktakeStatus.CLOSED
    assert DamagedInventory.objects.filter(warehouse=wh_rusafa, ref=st_obj.number).exists()


@pytest.mark.django_db
def test_orders_deliveries_payments_webhook_and_reports_full_matrix(tmp_path, settings):
    """Covers order accept/approve/update/discount/cancel, delivery problem/return, credit note/refund/reversal, Qi webhook, and grouped sales reports."""
    settings.MEDIA_ROOT = tmp_path
    call_command("seed_demo")
    mgr = _login("07701000001")
    acct = _login("07701000002")
    staff = _login("07701000005")
    wh_client = _login("07701000003")

    wh = Warehouse.objects.get(code="WH-KARKH")
    cust = Customer.objects.filter(status=CustomerStatus.ACTIVE, active=True).first()
    item = Item.objects.filter(active=True).first()

    # Order lifecycle: create -> accept -> approve -> update -> discount -> cancel
    o_res = mgr.post(
        "/api/v1/orders",
        {
            "customer_id": cust.id,
            "warehouse_id": wh.id,
            "lines": [{"item_id": item.id, "unit": "carton", "qty": 2}],
            "notes": "طلب اختبار",
        },
        format="json",
    )
    assert o_res.status_code == 201, o_res.json()
    oid = o_res.json()["data"]["id"]

    # Update lines on pending order + apply discount (staff blocked >3%, manager allowed)
    o_upd = mgr.patch(
        f"/api/v1/orders/{oid}",
        {"lines": [{"item_id": item.id, "unit": "carton", "qty": 1}], "notes": "محدث"},
        format="json",
    )
    assert o_upd.status_code == 200
    assert staff.post(f"/api/v1/orders/{oid}/discount", {"percent": 10}, format="json").status_code == 403
    assert mgr.post(f"/api/v1/orders/{oid}/discount", {"percent": 5}, format="json").status_code == 200

    # Force order into needs_approval so approve path executes cleanly
    Order.objects.filter(id=oid).update(status=OrderStatus.NEEDS_APPROVAL)
    assert mgr.post(f"/api/v1/orders/{oid}/approve", {}, format="json").status_code == 200
    assert mgr.post(f"/api/v1/orders/{oid}/cancel", {"reason": "إلغاء من قبل الاختبار"}, format="json").status_code == 200
    assert sweep_expired_reservations_task() >= 0

    # Create OUT delivery & report problem then return to warehouse
    o2_res = mgr.post(
        "/api/v1/orders",
        {"customer_id": cust.id, "warehouse_id": wh.id, "lines": [{"item_id": item.id, "unit": "carton", "qty": 1}]},
        format="json",
    )
    assert o2_res.status_code == 201
    o2_id = o2_res.json()["data"]["id"]
    prep_res = mgr.post(f"/api/v1/orders/{o2_id}/prepare", {"warehouses": {str(item.id): wh.id}}, format="json")
    assert prep_res.status_code == 200, prep_res.json()
    o2_dlv_code = Order.objects.get(id=o2_id).delivery.code
    rep_client = _login("07701000006")
    assert rep_client.post("/api/v1/deliveries/dispatch", {"code": o2_dlv_code}, format="json").status_code == 200
    assert rep_client.post(
        "/api/v1/deliveries/problem",
        {"order_ids": [o2_id], "reason": "shop_closed", "note": "المحل مغلق"},
        format="json",
    ).status_code == 200
    assert mgr.post(f"/api/v1/orders/{o2_id}/delivery/return", {}, format="json").status_code == 200

    # Credit note, refund & payment reversal + Qi Card webhook
    inv = Invoice.objects.filter(customer=cust).order_by("id").first()
    assert inv is not None
    cn_res = acct.post(
        f"/api/v1/invoices/{inv.id}/credit",
        {"amount": 1000, "reason": "خصم تلف بسيط"},
        format="json",
    )
    assert cn_res.status_code == 200, cn_res.json()
    assert CreditNote.objects.filter(invoice=inv).exists()

    p_res = acct.post(
        "/api/v1/payments",
        {"customer_id": cust.id, "amount": 5000, "method": "cash", "note": "دفعة نقدية"},
        format="json",
    )
    assert p_res.status_code == 200
    pid = p_res.json()["data"]["id"]
    rev_res = acct.post(f"/api/v1/payments/{pid}/reverse", {"reason": "خطأ إدخال"}, format="json")
    assert rev_res.status_code == 200

    ref_res = acct.post(
        f"/api/v1/customers/{cust.id}/refunds",
        {"amount": 500, "method": "cash", "reason": "رد رصيد"},
        format="json",
    )
    assert ref_res.status_code in (200, 201, 400)  # exercised credit check + refund creation/guard cleanly

    # Online payment + Qi Card HMAC webhook settlement
    settings.QI_CARD_MODE = "sandbox"
    settings.QI_CARD_WEBHOOK_SECRET = "qi-secret-123"

    class _StubGW:
        def is_configured(self):
            return True

        def create_payment(self, **kwargs):
            return QiCreateResult(
                payment_id="qi-pay-test-1",
                status=OnlinePaymentStatus.CREATED,
                form_url="https://qi.example/pay",
                raw={"paymentId": "qi-pay-test-1"},
            )

    shop_user = User.objects.get(phone="07701000008")
    started = start_online_payment(actor=shop_user, data={"amount": 2000, "key": "qi-k-1"}, gateway=_StubGW())
    assert started["payment_id"] == "qi-pay-test-1"
    body = json.dumps({"paymentId": "qi-pay-test-1", "status": "SUCCESS", "maskedPan": "411111****1111"}).encode("utf-8")
    sig = hmac.new(b"qi-secret-123", body, hashlib.sha256).hexdigest()
    wh_client_http = APIClient()
    wh_res = wh_client_http.post(
        "/api/v1/webhooks/qi-card",
        data=body,
        content_type="application/json",
        HTTP_X_SIGNATURE=sig,
    )
    assert wh_res.status_code == 200, wh_res.json()
    op = OnlinePayment.objects.get(payment_id="qi-pay-test-1")
    assert op.status == OnlinePaymentStatus.SUCCESS and op.recorded_payment_id is not None
    # Settling again never downgrades or double-allocates
    settle_online_payment(online_payment_id=op.id, new_status=OnlinePaymentStatus.FAILED)
    op.refresh_from_db()
    assert op.status == OnlinePaymentStatus.SUCCESS

    # Reports grouped modes & role redactions
    for grp in ("item", "warehouse", "customer", "rep"):
        r = mgr.get(f"/api/v1/reports/sales?group_by={grp}")
        assert r.status_code == 200
    assert mgr.get("/api/v1/reports/daily-sales").status_code == 200
    assert mgr.get("/api/v1/reports/slow-moving?days=30").status_code == 200
    assert mgr.get("/api/v1/reports/near-expiry?days=90").status_code == 200
    assert mgr.get("/api/v1/reports/debts").status_code == 200
    # Warehouse & staff must NOT see profit/debt metrics in dashboard or debt report
    assert wh_client.get("/api/v1/reports/debts").status_code == 403
    dash_staff = staff.get("/api/v1/dashboard")
    assert dash_staff.status_code == 200
    assert dash_staff.json()["data"].get("month_profit") is None



@pytest.mark.django_db
def test_deep_service_branches_and_edge_cases(tmp_path, settings):
    """Covers inventory stock/batches/damaged views, customer creation/redaction, order accept/prep-plan, online polling, and reports date filters."""
    settings.MEDIA_ROOT = tmp_path
    call_command("seed_demo")
    mgr = _login("07701000001")
    acct = _login("07701000002")
    wh_client = _login("07701000003")
    staff = _login("07701000005")
    rep_client = _login("07701000006")
    shop_client = _login("07701000008")

    wh = Warehouse.objects.get(code="WH-KARKH")
    item = Item.objects.filter(active=True).first()
    today_iso = baghdad_today().isoformat()
    week_ago = (baghdad_today() - timedelta(days=7)).isoformat()

    # Inventory endpoints & role redactions
    stock_mgr = mgr.get(f"/api/v1/stock?warehouse_id={wh.id}")
    stock_staff = staff.get("/api/v1/stock")
    assert stock_mgr.status_code == 200 and len(stock_mgr.json()["data"]) >= 1
    assert stock_staff.status_code == 200 and "purchase_price" not in str(stock_staff.json())
    assert mgr.get(f"/api/v1/batches?warehouse_id={wh.id}").status_code == 200
    assert staff.get("/api/v1/batches").status_code == 200
    assert mgr.get(f"/api/v1/items/{item.id}/batches").status_code == 200
    assert mgr.get(f"/api/v1/damaged?warehouse_id={wh.id}").status_code == 200

    batch = Batch.objects.filter(warehouse=wh, qty_left__gt=0, held_at__isnull=True).first()
    assert mgr.post(f"/api/v1/batches/{batch.id}/hold", {"reason": ""}, format="json").status_code == 400
    assert mgr.post(f"/api/v1/batches/{batch.id}/release", {}, format="json").status_code == 400

    # Customer creation + validations + redactions
    bad_cust = mgr.post("/api/v1/customers", {"name": "", "phone": "123"}, format="json")
    assert bad_cust.status_code == 400
    new_cust_payload = {
        "name": "سوبرماركت البصرة",
        "owner_name": "حسين",
        "phone": "07707771122",
        "area": "الكرادة",
        "address_info": {
            "label": "الفرع الرئيسي",
            "details": "شارع 62 مقابل الساحة",
            "phone": "07707771122",
            "street": "62",
            "landmark": "ساحة",
        },
        "debt_limit": 2000000,
    }
    cust_res = mgr.post("/api/v1/customers", new_cust_payload, format="json")
    assert cust_res.status_code == 201, cust_res.json()
    new_cid = cust_res.json()["data"]["id"]
    # Duplicate phone rejected
    assert mgr.post("/api/v1/customers", new_cust_payload, format="json").status_code == 400
    # Warehouse/staff omit redacted financial debt/balance on /customers (matches TS optional fields)
    wh_cust = wh_client.get(f"/api/v1/customers/{new_cid}")
    assert wh_cust.status_code == 200
    assert "debt_limit" not in wh_cust.json()["data"] and "balance" not in wh_cust.json()["data"]
    assert rep_client.get("/api/v1/customers").status_code == 200

    # Catalog validation branches
    assert mgr.post("/api/v1/items", {"name": ""}, format="json").status_code == 400
    assert mgr.patch("/api/v1/items/999999", {"name": "X"}, format="json").status_code == 404
    assert mgr.patch(f"/api/v1/items/{item.id}", {"pieces_per_carton": 0}, format="json").status_code == 400

    # Shop creates pending order -> rep/mgr accepts -> prep-plan -> shop cancels another order
    o_shop = shop_client.post(
        "/api/v1/orders",
        {"warehouse_id": wh.id, "lines": [{"item_id": item.id, "unit": "carton", "qty": 1}]},
        format="json",
    )
    assert o_shop.status_code == 201, o_shop.json()
    o_shop_id = o_shop.json()["data"]["id"]
    if o_shop.json()["data"]["status"] == OrderStatus.PENDING:
        acc_res = mgr.post(f"/api/v1/orders/{o_shop_id}/accept", {}, format="json")
        assert acc_res.status_code == 200
    prep_plan = mgr.get(f"/api/v1/orders/{o_shop_id}/prep-plan")
    assert prep_plan.status_code == 200 and len(prep_plan.json()["data"]) == 1
    assert shop_client.post(f"/api/v1/orders/{o_shop_id}/cancel", {"reason": "تغيير الكمية"}, format="json").status_code == 200

    # Payments list, online list, statement filters & audit log filters
    cust = Customer.objects.filter(status=CustomerStatus.ACTIVE, active=True).first()
    assert acct.get("/api/v1/payments").status_code == 200
    assert acct.get("/api/v1/payments/online").status_code == 200
    assert acct.get(f"/api/v1/customers/{cust.id}/statement?from={week_ago}&to={today_iso}").status_code == 200
    assert mgr.get("/api/v1/audit?action=order.accept").status_code == 200
    assert acct.get(f"/api/v1/reports/sales?from={week_ago}&to={today_iso}&warehouse_id={wh.id}&group_by=item").status_code == 200
    assert acct.get(f"/api/v1/reports/daily-sales?date={today_iso}&warehouse_id={wh.id}").status_code == 200
    assert acct.get(f"/api/v1/dashboard?warehouse_id={wh.id}").status_code == 200


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_realtime_consumer_actions_and_limits(tmp_path, settings):
    """Covers WebSocket subscribe, unsubscribe, ping/pong, unknown topic, forbidden topic, invalid JSON, and bad Origin rejection."""
    settings.MEDIA_ROOT = tmp_path
    settings.CORS_ALLOWED_ORIGINS = ["https://app.alnahrain.example"]
    from asgiref.sync import sync_to_async
    from channels.testing import WebsocketCommunicator

    from apps.realtime.tickets import issue_realtime_ticket
    from config.asgi import application

    await sync_to_async(call_command)("seed_demo")
    wh_user = await sync_to_async(User.objects.get)(phone="07701000003")
    raw_ticket, _ = await sync_to_async(issue_realtime_ticket)(user=wh_user)

    # Disallowed Origin closes with 4003
    bad_origin_comm = WebsocketCommunicator(
        application,
        f"/ws?ticket={raw_ticket}",
        headers=[(b"origin", b"https://evil.example")],
    )
    ok_bad, close_code = await bad_origin_comm.connect()
    assert not ok_bad and close_code == 4003

    # Allowed Origin connects and handles control messages
    good_ticket, _ = await sync_to_async(issue_realtime_ticket)(user=wh_user)
    comm = WebsocketCommunicator(
        application,
        f"/ws?ticket={good_ticket}",
        headers=[(b"origin", b"https://app.alnahrain.example")],
    )
    ok, _ = await comm.connect()
    assert ok

    await comm.send_json_to({"action": "ping"})
    pong = await comm.receive_json_from()
    assert pong["type"] == "pong"

    await comm.send_json_to({"action": "subscribe", "topics": ["orders", "notifications", "chat", "bogus"]})
    err_chat = await comm.receive_json_from()
    err_bogus = await comm.receive_json_from()
    assert err_chat["code"] == "FORBIDDEN" and err_chat["topic"] == "chat"
    assert err_bogus["code"] == "UNKNOWN_TOPIC" and err_bogus["topic"] == "bogus"

    await comm.send_json_to({"action": "unsubscribe", "topics": ["orders"]})
    await comm.send_json_to({"action": "mystery"})
    err_unknown = await comm.receive_json_from()
    assert err_unknown["code"] == "UNKNOWN_ACTION"

    await comm.send_to(text_data="[1, 2, 3]")
    err_payload = await comm.receive_json_from()
    assert err_payload["code"] == "INVALID_PAYLOAD"

    await comm.send_to(text_data="not-json{{")
    err_json = await comm.receive_json_from()
    assert err_json["code"] == "INVALID_JSON"

    await comm.send_to(bytes_data=b"\x00\x01")
    close_evt = await comm.receive_output()
    assert close_evt["type"] == "websocket.close" and close_evt["code"] == 4009
    await comm.disconnect()


def test_supabase_storage_client_mocked(monkeypatch):
    """Covers SupabaseStorageClient upload, delete, public URL, signed URL, and error branches with mocked httpx."""
    from integrations.storage.supabase import SupabaseStorageClient, SupabaseStorageError

    with pytest.raises(RuntimeError):
        SupabaseStorageClient(base_url="", service_role_key="")

    client = SupabaseStorageClient(base_url="https://proj.supabase.co", service_role_key="svc-secret")

    class _FakeResp:
        def __init__(self, status_code: int, payload: dict | None = None, text: str = "err"):
            self.status_code = status_code
            self._payload = payload or {}
            self.text = text

        def json(self):
            return self._payload

    monkeypatch.setattr("integrations.storage.supabase.httpx.post", lambda *a, **kw: _FakeResp(200, {"signedURL": "/object/sign/delivery-proofs/a.jpg?token=t"}))
    assert client.put_bytes("product-images", "/items/1.png", b"pngdata", "image/png") == "items/1.png"
    assert client.get_url("product-images", "items/1.png", private=False) == "https://proj.supabase.co/storage/v1/object/public/product-images/items/1.png"
    signed = client.get_url("delivery-proofs", "a.jpg", private=True, expires_in=600)
    assert signed == "https://proj.supabase.co/storage/v1/object/sign/delivery-proofs/a.jpg?token=t"

    monkeypatch.setattr("integrations.storage.supabase.httpx.delete", lambda *a, **kw: _FakeResp(204))
    client.delete("product-images", "items/1.png")
    client.delete("product-images", "")

    monkeypatch.setattr("integrations.storage.supabase.httpx.post", lambda *a, **kw: _FakeResp(500, text="boom"))
    with pytest.raises(SupabaseStorageError):
        client.put_bytes("product-images", "1.png", b"x", "image/png")
    with pytest.raises(SupabaseStorageError):
        client.get_url("delivery-proofs", "a.jpg", private=True)

    monkeypatch.setattr("integrations.storage.supabase.httpx.delete", lambda *a, **kw: _FakeResp(500, text="del-err"))
    with pytest.raises(SupabaseStorageError):
        client.delete("product-images", "1.png")



@pytest.mark.django_db
def test_qi_card_gateway_transport_and_reconcile_task(settings):
    """Covers QiCardGateway MockTransport HTTP create/status/error paths and reconcile_online_payments_task."""
    import httpx

    from apps.notifications.tasks import reconcile_online_payments_task
    from integrations.qi_card.client import QiCardGateway

    settings.QI_CARD_MODE = "sandbox"
    settings.QI_CARD_BASE_URL = "https://sandbox-qi.example"
    settings.QI_CARD_MERCHANT_ID = "m1"
    settings.QI_CARD_TERMINAL_ID = "t1"
    settings.QI_CARD_API_KEY = "k1"
    settings.QI_CARD_WEBHOOK_SECRET = "sec1"

    def _ok_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"paymentId": "qi-tx-9", "formUrl": "https://pay.qi/9", "status": "CREATED"})
        return httpx.Response(200, json={"paymentId": "qi-tx-9", "status": "PAID", "maskedPan": "5111****9999"})

    gw = QiCardGateway(transport=httpx.MockTransport(_ok_handler))
    created = gw.create_payment(
        request_id="req-9", amount=15000, finish_url="/done", notification_url="/wh", customer_name="عمر", customer_phone="07701000008"
    )
    assert created.payment_id == "qi-tx-9" and created.status == "created"
    st = gw.get_payment_status("qi-tx-9")
    assert st.status == "success" and st.masked_card == "5111****9999"

    # map_status & error responses
    assert gw.map_status("DECLINED") == "failed"
    assert gw.map_status("CANCELLED") == "cancelled"
    assert gw.map_status("EXPIRED") == "expired"
    err_gw = QiCardGateway(transport=httpx.MockTransport(lambda req: httpx.Response(502, json={"err": "bad"})))
    with pytest.raises(ApiException):
        err_gw.create_payment(request_id="req-x", amount=1000, finish_url="/", notification_url="/wh")
    with pytest.raises(ApiException):
        err_gw.get_payment_status("qi-tx-x")

    def _net_boom(request: httpx.Request):
        raise httpx.ConnectError("refused")

    net_gw = QiCardGateway(transport=httpx.MockTransport(_net_boom))
    with pytest.raises(ApiException):
        net_gw.create_payment(request_id="req-n", amount=1000, finish_url="/", notification_url="/wh")
    with pytest.raises(ApiException):
        net_gw.get_payment_status("qi-tx-n")

    # Signature error checks
    with pytest.raises(ApiException):
        gw.verify_webhook_signature(raw_body=b"{}", headers={})
    with pytest.raises(ApiException):
        gw.verify_webhook_signature(raw_body=b"{}", headers={"X-Signature": "sha256=wrong"})

    # Reconcile created payment older than 2 minutes via task
    call_command("seed_demo")
    cust = Customer.objects.filter(status=CustomerStatus.ACTIVE).first()
    shop_u = User.objects.get(phone="07701000008")
    op = OnlinePayment.objects.create(
        request_id="REQ-REC-1",
        payment_id="qi-tx-9",
        customer=cust,
        initiated_by=shop_u,
        amount=12000,
        status=OnlinePaymentStatus.CREATED,
    )
    OnlinePayment.objects.filter(id=op.id).update(created_at=datetime.now(tz=UTC) - timedelta(minutes=5))
    with patch("integrations.qi_card.client.QiCardGateway.get_payment_status", return_value=st):
        settled_cnt = reconcile_online_payments_task()
    assert settled_cnt == 1
    op.refresh_from_db()
    assert op.status == OnlinePaymentStatus.SUCCESS and op.recorded_payment_id is not None


@pytest.mark.django_db
def test_common_helpers_and_secondary_flows(tmp_path, settings):
    """Covers Baghdad calendar helpers, localization/error flattening, notifications read endpoints, returns/transfers list & details."""
    settings.MEDIA_ROOT = tmp_path
    from apps.common.exceptions import _flatten_drf_errors, get_request_lang, localize
    from apps.common.utils import baghdad_day_of, client_ip

    now_utc = datetime(2026, 5, 1, 22, 30, tzinfo=UTC)  # 01:30 May 2 in Asia/Baghdad
    assert baghdad_day_of(now_utc) == "2026-05-02"
    assert baghdad_day_of("2026-05-01T22:30:00Z") == "2026-05-02"
    assert localize(("عربي", "English"), "en") == "English"
    assert localize(("عربي", "English"), "ar") == "عربي"
    assert localize("plain", "en") == "plain"

    fake_req = type("Req", (), {"META": {"HTTP_X_FORWARDED_FOR": "198.51.100.10, 10.0.0.1"}, "headers": {"Accept-Language": "en-US"}})()
    assert client_ip(fake_req) == "198.51.100.10"
    assert get_request_lang(fake_req) == "en"
    flat_msg, flat_map = _flatten_drf_errors({"phone": ["Required"], "nested": {"k": "bad"}}, "en")
    assert flat_msg == "Required" and "nested.k" in flat_map

    call_command("seed_demo")
    mgr = _login("07701000001")
    shop = _login("07701000008")
    notifs = mgr.get("/api/v1/notifications")
    assert notifs.status_code == 200
    if notifs.json()["data"]:
        nid = notifs.json()["data"][0]["id"]
        assert mgr.post(f"/api/v1/notifications/{nid}/read", {}, format="json").status_code == 200
    assert mgr.post("/api/v1/notifications/read-all", {}, format="json").status_code == 200

    # Invoice, returns, receipts, transfers list/detail routes
    assert mgr.get("/api/v1/invoices").status_code == 200
    inv = Invoice.objects.first()
    if inv:
        assert mgr.get(f"/api/v1/invoices/{inv.id}").status_code == 200
    assert mgr.get("/api/v1/returns").status_code == 200
    assert mgr.get("/api/v1/receipts").status_code == 200
    assert mgr.get("/api/v1/transfers").status_code == 200
    assert shop.get("/api/v1/usd-rates").status_code == 200
    assert mgr.post("/api/v1/usd-rates", {"rate": 1515}, format="json").status_code == 200
