"""Tests for missing endpoints, ZainCash payments, OpenAPI schemas, and security handlers."""
from __future__ import annotations

import io
from datetime import UTC
from unittest.mock import patch

import pytest
from rest_framework import status
from rest_framework.test import APIClient

from apps.accounts.models import Role, User
from apps.catalog.models import Item
from apps.customers.models import Customer, ShopList
from apps.invoices.models import Invoice
from apps.notifications.models import PushSubscription
from apps.orders.models import Order
from apps.warehouses.models import Warehouse


@pytest.fixture
def api_client():
    return APIClient()


@pytest.fixture
def setup_data(db):
    manager = User.objects.create_user(
        phone="07700000001", password="password123", name="Manager Test", role=Role.MANAGER
    )
    keeper = User.objects.create_user(
        phone="07700000002", password="password123", name="Keeper Test", role=Role.WAREHOUSE
    )
    rep = User.objects.create_user(
        phone="07700000003", password="password123", name="Rep Test", role=Role.REP, rep_kinds=["delivery"]
    )
    shop_user = User.objects.create_user(
        phone="07700000004", password="password123", name="Shop User", role=Role.SHOP
    )

    wh = Warehouse.objects.create(name="مخزن بغداد", name_en="Baghdad WH", code="BGD-1", capacity=1000)
    keeper.warehouse_id = wh.id
    keeper.save(update_fields=["warehouse_id"])

    from apps.accounts.models import PriceTier
    tier = PriceTier.objects.create(name="جملة", name_en="Wholesale")

    customer = Customer.objects.create(
        name="سوبرماركت البركة",
        phone="07700000004",
        tier=tier,
        status="active",
        active=True,
    )
    customer.memberships.create(user=shop_user, is_active_shop=True)

    item = Item.objects.create(
        name="حليب المراعي 1 لتر",
        name_en="Almarai Milk 1L",
        category="ألبان",
        brand="Almarai",
        pieces_per_carton=12,
        pack_name="كرتون",
        piece_name="علبة",
        sells="both",
        active=True,
    )
    item.prices.create(tier=tier, carton_price=24000, piece_price=2000)

    return {
        "manager": manager,
        "keeper": keeper,
        "rep": rep,
        "shop_user": shop_user,
        "warehouse": wh,
        "customer": customer,
        "item": item,
    }


# ========================================================
# 1. GET /catalog — public, NO auth, active items only
# ========================================================
@pytest.mark.django_db
def test_public_catalog_endpoint(api_client, setup_data):
    # Unauthenticated GET
    res = api_client.get("/api/v1/catalog")
    assert res.status_code == status.HTTP_200_OK
    body = res.json()
    assert body["success"] is True
    assert isinstance(body["data"], list)
    assert len(body["data"]) >= 1

    item_data = next((x for x in body["data"] if x["id"] == setup_data["item"].id), None)
    assert item_data is not None
    assert item_data["name"] == "حليب المراعي 1 لتر"
    assert item_data["brand"] == "Almarai"
    assert item_data["pack_name"] == "كرتون"
    assert item_data["piece_name"] == "علبة"
    assert "prices" not in item_data
    assert "avg_cost" not in item_data
    assert "barcode" not in item_data


# ========================================================
# 2. POST /counter-sales — warehouse keeper counter sale
# ========================================================
@pytest.mark.django_db
def test_counter_sale_by_keeper(api_client, setup_data):
    keeper = setup_data["keeper"]
    wh = setup_data["warehouse"]
    item = setup_data["item"]

    # Seed batch for item
    from datetime import date, timedelta

    from apps.inventory.models import Batch
    Batch.objects.create(
        item=item,
        warehouse=wh,
        qty_received=100,
        qty_left=100,
        expiry_date=date.today() + timedelta(days=90),
        purchase_price=1000,
    )

    api_client.force_authenticate(user=keeper)
    payload = {
        "client_ref": "cnt-sale-001",
        "warehouse_id": wh.id,
        "tier_id": 1,
        "method": "cash",
        "buyer_name": "زبون نقدي مباشر",
        "buyer_phone": "07701234567",
        "lines": [{"item_id": item.id, "unit": "piece", "qty": 5}],
    }
    res = api_client.post("/api/v1/counter-sales", payload, format="json")
    assert res.status_code == status.HTTP_200_OK
    inv = res.json()["data"]
    assert inv["counter"] is not None
    assert inv["counter"]["buyer_name"] == "زبون نقدي مباشر"
    assert inv["status"] == "paid"

    # Authenticate as manager to verify debt fields (redacted from warehouse keeper)
    api_client.force_authenticate(user=setup_data["manager"])
    res_mgr = api_client.get(f"/api/v1/invoices/{inv['id']}")
    assert res_mgr.status_code == status.HTTP_200_OK
    inv_mgr = res_mgr.json()["data"]
    assert inv_mgr["paid"] == inv_mgr["total"]
    assert inv_mgr["remaining"] == 0

    # Idempotent re-post
    api_client.force_authenticate(user=keeper)
    res_dup = api_client.post("/api/v1/counter-sales", payload, format="json")
    assert res_dup.status_code == status.HTTP_200_OK
    assert res_dup.json()["data"]["id"] == inv["id"]


# ========================================================
# 3. DELETE /items/:id — hard vs soft delete & conflict guards
# ========================================================
@pytest.mark.django_db
def test_delete_item_hard_and_soft(api_client, setup_data):
    manager = setup_data["manager"]
    wh = setup_data["warehouse"]
    api_client.force_authenticate(user=manager)

    # 1. Item with stock refuses deletion (409)
    item_with_stock = Item.objects.create(
        name="صنف بمخزون", name_en="Stock Item", category="عام", pieces_per_carton=1
    )
    from datetime import date, timedelta

    from apps.inventory.models import Batch
    Batch.objects.create(
        item=item_with_stock,
        warehouse=wh,
        qty_received=10,
        qty_left=10,
        expiry_date=date.today() + timedelta(days=60),
        purchase_price=500,
    )
    res_refuse = api_client.delete(f"/api/v1/items/{item_with_stock.id}")
    assert res_refuse.status_code == status.HTTP_409_CONFLICT

    # 2. Never used item -> hard delete ("removed")
    fresh_item = Item.objects.create(
        name="صنف جديد لم يستخدم", name_en="Fresh Item", category="عام", pieces_per_carton=1
    )
    res_hard = api_client.delete(f"/api/v1/items/{fresh_item.id}")
    assert res_hard.status_code == status.HTTP_200_OK
    assert res_hard.json()["data"]["result"] == "removed"
    assert not Item.objects.filter(id=fresh_item.id).exists()

    # 3. Previously used in an invoice -> soft delete ("hidden")
    from django.utils import timezone
    used_item = Item.objects.create(
        name="صنف مباع سابقاً", name_en="Sold Item", category="عام", pieces_per_carton=1
    )
    cust = setup_data["customer"]
    order_prev = Order.objects.create(
        number="ORD-USED-001",
        customer=cust,
        warehouse=wh,
        created_by=manager,
    )
    inv = Invoice.objects.create(
        number="INV-TEST-001",
        order=order_prev,
        customer=cust,
        warehouse=wh,
        subtotal=1000,
        total=1000,
        delivered_at=timezone.now(),
    )
    inv.lines.create(warehouse=wh, item=used_item, unit="piece", qty=1, pieces=1, unit_price=1000, total=1000)

    res_soft = api_client.delete(f"/api/v1/items/{used_item.id}")
    assert res_soft.status_code == status.HTTP_200_OK
    assert res_soft.json()["data"]["result"] == "hidden"
    used_item.refresh_from_db()
    assert used_item.deleted is True
    assert used_item.active is False


# ========================================================
# 4. POST /items/:id/video — link or upload
# ========================================================
@pytest.mark.django_db
def test_item_video_endpoint(api_client, setup_data):
    manager = setup_data["manager"]
    item = setup_data["item"]
    api_client.force_authenticate(user=manager)

    # 1. JSON https link
    res_json = api_client.post(
        f"/api/v1/items/{item.id}/video",
        {"video": "https://cdn.example.com/videos/product1.mp4"},
        format="json",
    )
    assert res_json.status_code == status.HTTP_200_OK
    assert res_json.json()["data"]["video"] == "https://cdn.example.com/videos/product1.mp4"

    # 2. File upload
    dummy_video = io.BytesIO(b"fake-mp4-video-stream-content")
    dummy_video.name = "sample.mp4"
    res_file = api_client.post(
        f"/api/v1/items/{item.id}/video",
        {"file": dummy_video},
        format="multipart",
    )
    assert res_file.status_code == status.HTTP_200_OK
    assert res_file.json()["data"]["video"].endswith(".mp4")


# ========================================================
# 5. POST /orders/:id/rating & GET /reports/delivery-ratings
# ========================================================
@pytest.mark.django_db
def test_order_delivery_rating_and_report(api_client, setup_data):
    shop_user = setup_data["shop_user"]
    manager = setup_data["manager"]
    rep = setup_data["rep"]
    cust = setup_data["customer"]
    wh = setup_data["warehouse"]

    from apps.deliveries.models import Delivery
    order = Order.objects.create(
        number="ORD-TEST-RTG",
        customer=cust,
        warehouse=wh,
        status="confirmed",
        total=50000,
        created_by=shop_user,
        rep=rep,
    )
    from datetime import UTC, datetime
    Delivery.objects.create(
        order=order,
        code="RTG-123",
        status="delivered",
        delivered_at=datetime.now(UTC),
        rep=rep,
    )

    # Shop user rates 2 stars
    api_client.force_authenticate(user=shop_user)
    res_rate = api_client.post(
        f"/api/v1/orders/{order.id}/rating",
        {"stars": 2, "note": "التوصيل تأخر ساعتين"},
        format="json",
    )
    assert res_rate.status_code == status.HTTP_200_OK
    order_data = res_rate.json()["data"]
    assert order_data["rating"]["stars"] == 2
    assert order_data["rating"]["note"] == "التوصيل تأخر ساعتين"

    # Manager checks report
    api_client.force_authenticate(user=manager)
    res_rep = api_client.get("/api/v1/reports/delivery-ratings")
    assert res_rep.status_code == status.HTTP_200_OK
    report = res_rep.json()["data"]
    assert report["count"] >= 1
    assert report["spread"][1] >= 1
    assert len(report["latest"]) >= 1


# ========================================================
# 6. /shop-lists CRUD
# ========================================================
@pytest.mark.django_db
def test_shop_lists_crud(api_client, setup_data):
    shop_user = setup_data["shop_user"]
    item = setup_data["item"]
    api_client.force_authenticate(user=shop_user)

    # 1. Create list
    res_create = api_client.post(
        "/api/v1/shop-lists",
        {
            "name": "طلبية الأسبوع الأساسية",
            "lines": [{"item_id": item.id, "unit": "carton", "qty": 3}],
        },
        format="json",
    )
    assert res_create.status_code == status.HTTP_201_CREATED
    slist = res_create.json()["data"]
    assert slist["name"] == "طلبية الأسبوع الأساسية"
    assert len(slist["lines"]) == 1

    # 2. Get lists
    res_list = api_client.get("/api/v1/shop-lists")
    assert res_list.status_code == status.HTTP_200_OK
    assert len(res_list.json()["data"]) >= 1

    # 3. Update list
    res_update = api_client.patch(
        f"/api/v1/shop-lists/{slist['id']}",
        {"name": "طلبية معدلة"},
        format="json",
    )
    assert res_update.status_code == status.HTTP_200_OK
    assert res_update.json()["data"]["name"] == "طلبية معدلة"

    # 4. Delete list
    res_del = api_client.delete(f"/api/v1/shop-lists/{slist['id']}")
    assert res_del.status_code == status.HTTP_200_OK
    assert not ShopList.objects.filter(id=slist["id"]).exists()


# ========================================================
# 7. Push Subscriptions
# ========================================================
@pytest.mark.django_db
def test_push_subscriptions(api_client, setup_data):
    shop_user = setup_data["shop_user"]
    api_client.force_authenticate(user=shop_user)

    payload = {
        "endpoint": "https://fcm.googleapis.com/fcm/send/test-sub-123",
        "keys": {"p256dh": "dummy-p256dh-key", "auth": "dummy-auth-key"},
    }
    res_sub = api_client.post("/api/v1/push-subscriptions", payload, format="json")
    assert res_sub.status_code == status.HTTP_200_OK
    assert PushSubscription.objects.filter(endpoint=payload["endpoint"]).exists()

    res_unsub = api_client.post(
        "/api/v1/push-subscriptions/delete",
        {"endpoint": payload["endpoint"]},
        format="json",
    )
    assert res_unsub.status_code == status.HTTP_200_OK
    assert not PushSubscription.objects.filter(endpoint=payload["endpoint"]).exists()


# ========================================================
# 8. ZainCash Payment Integration
# ========================================================
@pytest.mark.django_db
def test_zaincash_online_payment(api_client, setup_data):
    shop_user = setup_data["shop_user"]
    api_client.force_authenticate(user=shop_user)

    # 1. Less than 1,000 IQD rejected
    res_low = api_client.post(
        "/api/v1/payments/online",
        {"amount": 500, "provider": "zaincash", "key": "zc-key-1"},
        format="json",
    )
    assert res_low.status_code == status.HTTP_400_BAD_REQUEST

    # 2. Init transaction success with mocked ZainCash gateway
    with patch("integrations.zaincash.client.ZainCashClient.init_transaction") as mock_init:
        mock_init.return_value = {
            "transaction_id": "zc-tx-8899",
            "redirect_url": "https://pg-api-uat.zaincash.iq/pay/zc-tx-8899",
        }
        res = api_client.post(
            "/api/v1/payments/online",
            {
                "amount": 25000,
                "provider": "zaincash",
                "key": "zc-key-2",
                "return_url": "/statement",
            },
            format="json",
        )
        assert res.status_code == status.HTTP_200_OK
        op_data = res.json()["data"]
        assert op_data["provider"] == "zaincash"
        assert op_data["gateway_id"] == "zc-tx-8899"
        assert "zc-tx-8899" in op_data["form_url"]

    # 3. Inquiry status settlement
    with patch("integrations.zaincash.client.ZainCashClient.inquire_transaction") as mock_inq:
        mock_inq.return_value = {
            "status": "SUCCESS",
            "phone": "07801234567",
            "raw": {"status": "SUCCESS"},
        }
        res_get = api_client.get(f"/api/v1/payments/online/{op_data['payment_id']}")
        assert res_get.status_code == status.HTTP_200_OK
        settled_data = res_get.json()["data"]
        assert settled_data["status"] == "success"
        assert settled_data["recorded_payment_id"] is not None


# ========================================================
# 9. Security: Non-existent URL returns clean JSON 404
# ========================================================
@pytest.mark.django_db
def test_security_404_json_handler(api_client):
    res = api_client.get("/api/v1/some-non-existent-route-999")
    assert res.status_code == status.HTTP_404_NOT_FOUND
    body = res.json()
    assert body["success"] is False
    assert body["code"] == "NOT_FOUND"
    # Ensure no Django debug traceback or route list
    assert "traceback" not in str(res.content).lower()
    assert "using the urlconf defined in" not in str(res.content).lower()
