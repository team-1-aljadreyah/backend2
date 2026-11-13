"""Cross-domain security, RBAC/IDOR matrix, auth throttling, seed_demo, and OpenAPI schema tests."""
from __future__ import annotations

from io import StringIO

import pytest
from django.core.cache import cache
from django.core.management import CommandError, call_command
from rest_framework.test import APIClient

from apps.accounts.models import Role, User
from apps.accounts.throttles import LoginRateThrottle
from apps.common.utils import decrypt_secret
from apps.customers.models import Customer
from apps.deliveries.models import Delivery
from apps.invoices.models import Invoice
from apps.orders.models import Order
from apps.payments.models import Payment
from apps.receipts.models import Receipt
from apps.warehouses.models import Warehouse


def _login(phone: str, password: str = "Demo12345!") -> APIClient:
    client = APIClient()
    res = client.post("/api/v1/auth/login", {"phone": phone, "password": password}, format="json")
    assert res.status_code == 200, res.json()
    tok = res.json()["data"]["token"]
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {tok}")
    return client


@pytest.mark.django_db
def test_seed_demo_is_idempotent_and_blocked_in_production(tmp_path, monkeypatch, settings):
    settings.MEDIA_ROOT = tmp_path

    # Production environment refusal
    monkeypatch.setenv("DJANGO_ENV", "production")
    with pytest.raises(CommandError, match="production"):
        call_command("seed_demo")
    monkeypatch.delenv("DJANGO_ENV", raising=False)

    out1 = StringIO()
    call_command("seed_demo", stdout=out1)
    assert "Demo seed complete" in out1.getvalue()

    snapshot = {
        "users": User.objects.count(),
        "warehouses": Warehouse.objects.count(),
        "customers": Customer.objects.count(),
        "receipts": Receipt.objects.count(),
        "orders": Order.objects.count(),
        "invoices": Invoice.objects.count(),
        "payments": Payment.objects.count(),
    }
    assert snapshot["users"] >= 8
    assert snapshot["warehouses"] == 2
    assert snapshot["invoices"] >= 1
    assert snapshot["payments"] >= 1

    # Second run must not duplicate rows
    out2 = StringIO()
    call_command("seed_demo", stdout=out2)
    assert User.objects.count() == snapshot["users"]
    assert Warehouse.objects.count() == snapshot["warehouses"]
    assert Customer.objects.count() == snapshot["customers"]
    assert Receipt.objects.count() == snapshot["receipts"]
    assert Order.objects.count() == snapshot["orders"]
    assert Invoice.objects.count() == snapshot["invoices"]
    assert Payment.objects.count() == snapshot["payments"]


@pytest.mark.django_db
def test_cross_domain_rbac_idor_and_confidential_redactions(tmp_path, settings):
    settings.MEDIA_ROOT = tmp_path
    call_command("seed_demo")

    mgr = _login("07701000001")
    acct = _login("07701000002")
    keeper1 = _login("07701000003")
    keeper2 = _login("07701000004")
    staff1 = _login("07701000005")
    sales_rep = _login("07701000006")
    driver = _login("07701000007")
    shop = _login("07701000008")

    cust_basra = Customer.objects.get(phone="07701000009")
    wh1 = Warehouse.objects.get(code="WH-KARKH")
    wh2 = Warehouse.objects.get(code="WH-RUSAFA")
    inv1 = Invoice.objects.order_by("id").first()
    pay1 = Payment.objects.order_by("id").first()
    out_dlv = Delivery.objects.filter(status="out").select_related("order").first()
    delivered_ord = Order.objects.filter(status="confirmed").first()

    # 1. Manager-only admin settings & user directory/mutations
    for non_dir in (acct, keeper1, staff1, sales_rep, driver, shop):
        assert non_dir.get("/api/v1/users").status_code == 403
    for non_mgr in (acct, keeper1, staff1, sales_rep, driver, shop):
        assert (
            non_mgr.post(
                "/api/v1/users",
                {"phone": "07709999999", "name": "X", "role": "staff", "password": "Password123"},
                format="json",
            ).status_code
            == 403
        )
        assert non_mgr.patch("/api/v1/settings", {"near_expiry_days": 45}, format="json").status_code == 403

    # 2. Cost/profit confidentiality redaction across catalog & invoices
    for hidden_cost_client in (staff1, sales_rep, shop):
        items = hidden_cost_client.get("/api/v1/items").json()["data"]
        assert all("avg_cost" not in it for it in items)
        invs = hidden_cost_client.get("/api/v1/invoices").json()["data"]
        for inv in invs:
            assert "cost" not in inv
            assert "profit" not in inv

    for inv in staff1.get("/api/v1/invoices").json()["data"]:
        assert "paid" not in inv
        assert "remaining" not in inv

    assert mgr.get("/api/v1/items").json()["data"][0]["avg_cost"] > 0
    mgr_inv = mgr.get(f"/api/v1/invoices/{inv1.id}").json()["data"]
    assert mgr_inv["profit"] is not None
    assert mgr_inv["cost"] is not None

    # 3. Warehouse cross-scope isolation
    wh1_receipt = Receipt.objects.filter(warehouse=wh1).first()
    wh2_receipt = Receipt.objects.filter(warehouse=wh2).first()
    assert keeper1.get(f"/api/v1/receipts/{wh1_receipt.id}").status_code == 200
    assert keeper1.get(f"/api/v1/receipts/{wh2_receipt.id}").status_code == 403
    assert keeper2.get(f"/api/v1/receipts/{wh1_receipt.id}").status_code == 403
    assert shop.get("/api/v1/receipts").status_code == 403

    # 4. Delivery PIN strictly visible ONLY to owning active shop (and unrelated driver cannot view order)
    for client in (mgr, acct, keeper1, staff1, sales_rep):
        res = client.get(f"/api/v1/orders/{out_dlv.order_id}")
        assert res.status_code == 200
        assert res.json()["data"]["delivery"]["pin"] is None
    assert driver.get(f"/api/v1/orders/{out_dlv.order_id}").status_code == 403
    shop_ord = shop.get(f"/api/v1/orders/{out_dlv.order_id}").json()["data"]
    assert shop_ord["delivery"]["pin"] == decrypt_secret(out_dlv.pin_encrypted)

    # 5. Shop & Rep IDOR checks on orders / invoices / payments / statements
    assert shop.get(f"/api/v1/customers/{cust_basra.id}/statement").status_code == 403
    assert sales_rep.post(f"/api/v1/payments/{pay1.id}/reverse", {"reason": "test"}, format="json").status_code == 403
    assert keeper1.get(f"/api/v1/payments/{pay1.id}").status_code == 403
    assert staff1.get("/api/v1/payments").status_code == 403
    assert driver.get(f"/api/v1/orders/{delivered_ord.id}").status_code == 403

    # 6. Reports, Commissions & Audit RBAC
    assert shop.get("/api/v1/reports/debts").status_code == 403
    assert keeper1.get("/api/v1/reports/debts").status_code == 403
    assert sales_rep.post("/api/v1/commissions/close", {"month": "2026-11"}, format="json").status_code == 403
    assert keeper1.get("/api/v1/audit").status_code == 403
    assert staff1.get("/api/v1/chat").status_code == 200
    assert sales_rep.get("/api/v1/chat").status_code == 403
    assert acct.get("/api/v1/chat").status_code == 403


@pytest.mark.django_db
def test_auth_token_tampering_deactivation_and_login_rate_throttle(monkeypatch):
    user = User.objects.create_user(phone="07708880001", password="Password123", name="Test", role=Role.MANAGER)

    anon = APIClient()
    assert anon.get("/api/v1/items").status_code == 401

    anon.credentials(HTTP_AUTHORIZATION="Bearer invalid.jwt.token")
    assert anon.get("/api/v1/items").status_code == 401

    # Valid login then deactivate user -> token immediately rejected
    authed = _login("07708880001", "Password123")
    assert authed.get("/api/v1/users").status_code == 200
    user.active = False
    user.save(update_fields=["active"])
    assert authed.get("/api/v1/users").status_code == 401

    # Verify login throttling triggers HTTP 429 cleanly when limit is exceeded
    cache.clear()
    monkeypatch.setitem(LoginRateThrottle.THROTTLE_RATES, "login", "2/min")
    try:
        brute = APIClient()
        r1 = brute.post("/api/v1/auth/login", {"phone": "07708880001", "password": "wrong"}, format="json")
        r2 = brute.post("/api/v1/auth/login", {"phone": "07708880001", "password": "wrong"}, format="json")
        r3 = brute.post("/api/v1/auth/login", {"phone": "07708880001", "password": "wrong"}, format="json")
        assert r1.status_code == 401
        assert r2.status_code == 401
        assert r3.status_code == 429
    finally:
        cache.clear()


@pytest.mark.django_db
def test_openapi_schema_generation_and_validation(tmp_path):
    schema_file = tmp_path / "openapi.yml"
    call_command("spectacular", "--file", str(schema_file), "--validate")
    content = schema_file.read_text(encoding="utf-8")
    assert "/api/v1/auth/login" in content
    assert "/api/v1/orders" in content
    assert "/api/v1/payments" in content
