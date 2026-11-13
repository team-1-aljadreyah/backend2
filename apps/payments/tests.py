"""Tests for Invoices, Payments FIFO Allocation, Reversals, Credit Notes, Refunds, Returns, and Qi Card."""
from __future__ import annotations

import hashlib
import hmac
import json
from datetime import timedelta
from unittest.mock import patch

import httpx
import pytest
from django.test import override_settings
from rest_framework.test import APIClient

from apps.accounts.models import PriceTier, RepKind, Role, User
from apps.common.utils import baghdad_today
from apps.customers.models import Customer, CustomerStatus, ShopMembership
from apps.inventory.models import Batch, DamagedInventory, InventoryMovement, MovementKind
from apps.invoices.models import ReturnDoc
from apps.invoices.services import create_invoice_for_order
from apps.orders.models import Order, OrderLine, OrderSource, OrderStatus
from apps.payments.models import Payment
from apps.warehouses.models import Warehouse
from integrations.qi_card.client import QiCardGateway


def _c(phone: str) -> APIClient:
    user = User.objects.get(phone=phone)
    client = APIClient()
    client.force_authenticate(user=user)
    return client


@pytest.fixture
def phase6_setup(db):
    wh = Warehouse.objects.create(name="المخزن الرئيسي", name_en="Main WH", code="W1", active=True)
    tier = PriceTier.objects.create(name="جملة", name_en="Wholesale", sort_order=1)

    mgr = User.objects.create_user("07706000001", "Pass1234", name="Mgr", role=Role.MANAGER)
    acct = User.objects.create_user("07706000002", "Pass1234", name="Acct", role=Role.ACCOUNTANT)
    keeper = User.objects.create_user("07706000003", "Pass1234", name="Keeper", role=Role.WAREHOUSE, warehouse=wh)
    staff = User.objects.create_user("07706000004", "Pass1234", name="Staff", role=Role.STAFF, warehouse=wh)
    rep = User.objects.create_user("07706000005", "Pass1234", name="SalesRep", role=Role.REP, rep_kinds=[RepKind.SALES])
    other_rep = User.objects.create_user("07706000006", "Pass1234", name="OtherRep", role=Role.REP, rep_kinds=[RepKind.SALES])
    driver = User.objects.create_user("07706000007", "Pass1234", name="Driver", role=Role.REP, rep_kinds=[RepKind.DELIVERY])

    shop_u = User.objects.create_user("07706000010", "Pass1234", name="Shop Owner", role=Role.SHOP)
    cust = Customer.objects.create(
        name="سوبرماركت النور",
        owner_name="Shop Owner",
        phone="07706000010",
        city="بغداد",
        area="الكرادة",
        street="62",
        landmark="تقاطع",
        tier=tier,
        rep=rep,
        debt_limit=2_000_000,
        status=CustomerStatus.ACTIVE,
        active=True,
    )
    ShopMembership.objects.create(user=shop_u, customer=cust, is_primary=True, is_active_shop=True)

    return {
        "wh": wh,
        "tier": tier,
        "mgr": mgr,
        "acct": acct,
        "keeper": keeper,
        "staff": staff,
        "rep": rep,
        "other_rep": other_rep,
        "driver": driver,
        "shop_u": shop_u,
        "cust": cust,
    }


def _create_delivered_invoice(setup, *, item_id: int, qty: int, unit_price: int, suffix: str):
    wh = setup["wh"]
    cust = setup["cust"]
    rep = setup["rep"]
    driver = setup["driver"]
    mgr = setup["mgr"]
    total = qty * unit_price
    order = Order.objects.create(
        number=f"ORD-{suffix}",
        customer=cust,
        warehouse=wh,
        rep=rep,
        created_by=rep,
        source=OrderSource.REP,
        status=OrderStatus.CONFIRMED,
        subtotal=total,
        discount_amount=0,
        rounding=0,
        total=total,
    )
    ol = OrderLine.objects.create(
        order=order,
        item_id=item_id,
        warehouse=wh,
        unit="piece",
        qty=qty,
        pieces=qty,
        unit_price=unit_price,
        total=total,
    )
    from django.db import transaction

    with transaction.atomic():
        inv = create_invoice_for_order(
            order=order,
            delivered_line_specs=[
                {
                    "warehouse_id": wh.id,
                    "item_id": item_id,
                    "unit": "piece",
                    "qty": qty,
                    "pieces": qty,
                    "unit_price": unit_price,
                    "total": total,
                    "order_line": ol,
                }
            ],
            delivery_rep=driver,
            actor=mgr,
        )
    return inv


@pytest.mark.django_db
def test_fifo_payments_credit_notes_reversals_refunds_and_statement(phase6_setup):
    mgr = _c("07706000001")
    acct = _c("07706000002")
    keeper = _c("07706000003")
    rep = _c("07706000005")
    other_rep = _c("07706000006")
    shop = _c("07706000010")
    wh = phase6_setup["wh"]
    tier = phase6_setup["tier"]
    cust = phase6_setup["cust"]
    exp = (baghdad_today() + timedelta(days=90)).isoformat()

    item_id = mgr.post(
        "/api/v1/items",
        {"name": "شاي", "name_en": "Tea", "category": "شاي", "pieces_per_carton": 10, "prices": [{"tier_id": tier.id, "carton_price": 50000, "piece_price": 5000}]},
        format="json",
    ).json()["data"]["id"]
    keeper.post(
        "/api/v1/receipts",
        {"warehouse_id": wh.id, "lines": [{"item_id": item_id, "unit": "piece", "qty": 100, "expiry_date": exp, "purchase_price": 3000}]},
        format="json",
    )

    inv1 = _create_delivered_invoice(phase6_setup, item_id=item_id, qty=10, unit_price=5000, suffix="P6A")  # 50,000
    inv2 = _create_delivered_invoice(phase6_setup, item_id=item_id, qty=20, unit_price=5000, suffix="P6B")  # 100,000

    # Redaction & visibility: keeper doesn't see paid/remaining/cost/profit; shop/rep don't see cost/profit/batches; other_rep gets 403
    inv1_keeper = keeper.get(f"/api/v1/invoices/{inv1.id}").json()["data"]
    assert "paid" not in inv1_keeper and "cost" not in inv1_keeper and "profit" not in inv1_keeper
    inv1_shop = shop.get(f"/api/v1/invoices/{inv1.id}").json()["data"]
    assert "cost" not in inv1_shop and "profit" not in inv1_shop and "batches" not in inv1_shop["lines"][0]
    assert inv1_shop["remaining"] == 50000
    inv1_mgr = mgr.get(f"/api/v1/invoices/{inv1.id}").json()["data"]
    assert inv1_mgr["cost"] == 30000 and inv1_mgr["profit"] == 20000
    assert other_rep.get(f"/api/v1/invoices/{inv1.id}").status_code == 403

    # Credit note on inv1: cannot exceed remaining, reduces remaining by 10,000
    assert acct.post(f"/api/v1/invoices/{inv1.id}/credit", {"amount": 60000, "reason": "too high"}, format="json").status_code == 400
    assert rep.post(f"/api/v1/invoices/{inv1.id}/credit", {"amount": 10000, "reason": "not allowed"}, format="json").status_code == 403
    cn_res = acct.post(f"/api/v1/invoices/{inv1.id}/credit", {"amount": 10000, "reason": "Damaged box allowance"}, format="json")
    assert cn_res.status_code == 200
    assert cn_res.json()["data"]["amount"] == 10000
    assert cn_res.json()["data"]["by"]["id"] is not None
    assert "at" in cn_res.json()["data"]

    # Preview FIFO allocation for 90,000 -> 40,000 on inv1, 50,000 on inv2
    prev = rep.get(f"/api/v1/customers/{cust.id}/allocation?amount=90000").json()["data"]
    assert prev == [
        {"invoice_id": inv1.id, "number": inv1.number, "amount": 40000},
        {"invoice_id": inv2.id, "number": inv2.number, "amount": 50000},
    ]

    # Shop cannot manually post /payments; unrelated rep cannot; assigned rep can + idempotent client_ref
    assert shop.post("/api/v1/payments", {"customer_id": cust.id, "amount": 90000, "method": "cash"}, format="json").status_code == 403
    assert other_rep.post("/api/v1/payments", {"customer_id": cust.id, "amount": 90000, "method": "cash"}, format="json").status_code == 403
    p1 = rep.post(
        "/api/v1/payments",
        {"client_ref": "pay-ref-1", "customer_id": cust.id, "amount": 90000, "method": "cash"},
        format="json",
        HTTP_IDEMPOTENCY_KEY="idem-pay-1",
    )
    assert p1.status_code == 200
    p1_dup = rep.post(
        "/api/v1/payments",
        {"client_ref": "pay-ref-1", "customer_id": cust.id, "amount": 90000, "method": "cash"},
        format="json",
    )
    assert p1_dup.json()["data"]["id"] == p1.json()["data"]["id"]
    assert Payment.objects.count() == 1

    # Customer balance should now be 50,000 left on inv2
    assert mgr.get(f"/api/v1/customers/{cust.id}").json()["data"]["balance"] == 50000

    # Reversal restores balances & creates counter-entry; double reverse blocked
    rev = acct.post(f"/api/v1/payments/{p1.json()['data']['id']}/reverse", {"reason": "Wrong shop"}, format="json")
    assert rev.status_code == 200 and rev.json()["data"]["reversed"] is True
    assert acct.post(f"/api/v1/payments/{p1.json()['data']['id']}/reverse", {"reason": "Again"}, format="json").status_code == 400
    assert mgr.get(f"/api/v1/customers/{cust.id}").json()["data"]["balance"] == 140000

    # Overpayment (200,000 against 140,000 debt -> -60,000 advance credit), then refund 60,000
    p2 = mgr.post("/api/v1/payments", {"customer_id": cust.id, "amount": 200000, "method": "transfer"}, format="json").json()["data"]
    assert p2["unallocated_amount"] == 60000
    assert mgr.get(f"/api/v1/customers/{cust.id}").json()["data"]["balance"] == -60000

    assert mgr.post(f"/api/v1/customers/{cust.id}/refunds", {"amount": 70000, "method": "cash", "reason": "Too high"}, format="json").status_code == 400
    rf = mgr.post(f"/api/v1/customers/{cust.id}/refunds", {"amount": 60000, "method": "cash", "reason": "Excess payment"}, format="json")
    assert rf.status_code == 200
    assert rf.json()["data"]["by"]["id"] is not None and "at" in rf.json()["data"]
    assert mgr.get(f"/api/v1/customers/{cust.id}").json()["data"]["balance"] == 0

    st = shop.get(f"/api/v1/customers/{cust.id}/statement").json()["data"]
    assert st["balance"] == 0
    assert st["customer"]["id"] == cust.id
    assert len(st["rows"]) == 7  # 2 inv + 1 credit_note + 2 payments + 1 reversal + 1 refund


@pytest.mark.django_db
def test_returns_good_restock_damaged_recording_and_batch_trace(phase6_setup):
    mgr = _c("07706000001")
    keeper = _c("07706000003")
    wh = phase6_setup["wh"]
    tier = phase6_setup["tier"]
    exp = (baghdad_today() + timedelta(days=90)).isoformat()

    item_id = mgr.post(
        "/api/v1/items",
        {"name": "زيت", "name_en": "Oil", "category": "زيوت", "pieces_per_carton": 10, "prices": [{"tier_id": tier.id, "carton_price": 20000, "piece_price": 2000}]},
        format="json",
    ).json()["data"]["id"]
    keeper.post(
        "/api/v1/receipts",
        {"warehouse_id": wh.id, "lines": [{"item_id": item_id, "unit": "piece", "qty": 20, "expiry_date": exp, "purchase_price": 1200}]},
        format="json",
    )
    batch = Batch.objects.get(item_id=item_id, warehouse=wh)

    inv = _create_delivered_invoice(phase6_setup, item_id=item_id, qty=10, unit_price=2000, suffix="RET1")
    batch.refresh_from_db()
    assert batch.qty_left == 10

    # Collect 10,000 first so commission_base clawback can be verified
    mgr.post("/api/v1/payments", {"customer_id": phase6_setup["cust"].id, "amount": 10000, "method": "cash"}, format="json")

    # Exceeding delivered qty -> RETURN_TOO_MUCH
    bad_ret = mgr.post(
        "/api/v1/returns",
        {"invoice_id": inv.id, "lines": [{"item_id": item_id, "unit": "piece", "qty": 11, "condition": "good"}]},
        format="json",
    )
    assert bad_ret.status_code == 400 and bad_ret.json()["code"] == "RETURN_TOO_MUCH"

    # Return 4 good + 2 damaged = 6 pieces (12,000 IQD) -> reduces net due to 8,000, so commission_base clawback = 2,000!
    ret = mgr.post(
        "/api/v1/returns",
        {
            "client_ref": "ret-ref-1",
            "invoice_id": inv.id,
            "note": "Mixed return",
            "lines": [
                {"item_id": item_id, "unit": "piece", "qty": 4, "condition": "good"},
                {"item_id": item_id, "unit": "piece", "qty": 2, "condition": "damaged"},
            ],
        },
        format="json",
    )
    assert ret.status_code == 200
    ret_data = ret.json()["data"]
    assert ret_data["amount"] == 12000
    assert "commission_base" not in ret_data and "cost" not in ret_data
    assert ReturnDoc.objects.get(id=ret_data["id"]).commission_base == 2000

    # Good 4 pieces returned to original batch (10 + 4 = 14); damaged 2 recorded in DamagedInventory
    batch.refresh_from_db()
    assert batch.qty_left == 14
    assert InventoryMovement.objects.filter(kind=MovementKind.RETURN_GOOD, batch=batch, qty_delta=4).exists()
    assert DamagedInventory.objects.filter(item_id=item_id, batch=batch, pieces=2).exists()

    # Batch trace shows delivered 10 and returned 6
    trace = mgr.get(f"/api/v1/batches/{batch.id}/trace").json()["data"]
    assert len(trace) == 1 and trace[0]["pieces"] == 10 and trace[0]["returned"] == 6


@pytest.mark.django_db
def test_qi_card_gateway_security_and_webhook_settlement(phase6_setup):
    shop = _c("07706000010")
    mgr = _c("07706000001")
    keeper = _c("07706000003")
    wh = phase6_setup["wh"]
    tier = phase6_setup["tier"]
    exp = (baghdad_today() + timedelta(days=90)).isoformat()

    item_id = mgr.post(
        "/api/v1/items",
        {"name": "رز", "name_en": "Rice", "category": "حبوب", "pieces_per_carton": 10, "prices": [{"tier_id": tier.id, "carton_price": 40000, "piece_price": 4000}]},
        format="json",
    ).json()["data"]["id"]
    keeper.post(
        "/api/v1/receipts",
        {"warehouse_id": wh.id, "lines": [{"item_id": item_id, "unit": "piece", "qty": 20, "expiry_date": exp, "purchase_price": 3000}]},
        format="json",
    )
    inv = _create_delivered_invoice(phase6_setup, item_id=item_id, qty=10, unit_price=4000, suffix="QI1")  # 40,000

    # Disabled gateway refuses to fake online payment (503 BAD_STATE)
    with override_settings(QI_CARD_MODE="disabled"):
        disabled_res = shop.post("/api/v1/payments/online", {"amount": 40000}, format="json")
        assert disabled_res.status_code == 503

    def _mock_transport(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                200,
                json={"paymentId": "qi_pay_abc123", "formUrl": "https://sandbox.qi.iq/pay/qi_pay_abc123", "status": "CREATED"},
            )
        return httpx.Response(200, json={"paymentId": "qi_pay_abc123", "status": "CREATED"})

    secret = "test-qi-webhook-secret-987"
    with override_settings(
        QI_CARD_MODE="sandbox",
        QI_CARD_BASE_URL="https://sandbox.qi.iq",
        QI_CARD_MERCHANT_ID="M01",
        QI_CARD_TERMINAL_ID="T01",
        QI_CARD_API_KEY="api-key-1",
        QI_CARD_WEBHOOK_SECRET=secret,
    ):
        gw = QiCardGateway(transport=httpx.MockTransport(_mock_transport))
        with patch("apps.payments.services.QiCardGateway", return_value=gw):
            start_res = shop.post(
                "/api/v1/payments/online",
                {"amount": 40000, "return_url": "http://localhost:3000/statement"},
                format="json",
            )
            assert start_res.status_code == 200
            op = start_res.json()["data"]
            assert op["payment_id"] == "qi_pay_abc123"
            assert op["status"] == "created"

            # Webhook without signature or forged signature -> rejected 403
            anon = APIClient()
            payload_bytes = json.dumps({"paymentId": "qi_pay_abc123", "status": "SUCCESS", "maskedPan": "****4242"}).encode("utf-8")
            assert anon.post("/api/v1/webhooks/qi-card", data=payload_bytes, content_type="application/json").status_code == 403
            assert anon.post(
                "/api/v1/webhooks/qi-card",
                data=payload_bytes,
                content_type="application/json",
                HTTP_X_QI_SIGNATURE="deadbeef",
            ).status_code == 403

            # Valid HMAC-SHA256 webhook settles once and creates FIFO Payment
            sig = hmac.new(secret.encode("utf-8"), payload_bytes, hashlib.sha256).hexdigest()
            ok_wh = anon.post(
                "/api/v1/webhooks/qi-card",
                data=payload_bytes,
                content_type="application/json",
                HTTP_X_QI_SIGNATURE=f"sha256={sig}",
            )
            assert ok_wh.status_code == 200 and ok_wh.json()["data"]["status"] == "success"
            pay_id = ok_wh.json()["data"]["payment_id"]
            assert pay_id is not None

            # Replay webhook idempotently returns same payment_id without duplicating Payment
            replay_wh = anon.post(
                "/api/v1/webhooks/qi-card",
                data=payload_bytes,
                content_type="application/json",
                HTTP_X_QI_SIGNATURE=sig,
            )
            assert replay_wh.status_code == 200 and replay_wh.json()["data"]["payment_id"] == pay_id
            assert Payment.objects.filter(customer=phase6_setup["cust"]).count() == 1

            inv_after = mgr.get(f"/api/v1/invoices/{inv.id}").json()["data"]
            assert inv_after["paid"] == 40000 and inv_after["remaining"] == 0 and inv_after["status"] == "paid"
            p_detail = shop.get(f"/api/v1/payments/{pay_id}").json()["data"]
            assert p_detail["gateway"]["card"] == "****4242" and p_detail["gateway"]["status"] == "success"
