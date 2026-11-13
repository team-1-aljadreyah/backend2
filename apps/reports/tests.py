"""Tests for Commissions, Month Close guard, USD Exchange Rates, Dashboard & Reports RBAC/redactions."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from django.db import transaction
from rest_framework.test import APIClient

from apps.accounts.models import PriceTier, RepKind, Role, SystemSetting, User
from apps.common.utils import baghdad_today
from apps.customers.models import Customer, CustomerStatus, ShopMembership
from apps.invoices.services import create_invoice_for_order
from apps.orders.models import Order, OrderLine, OrderSource, OrderStatus
from apps.payments.models import Payment
from apps.warehouses.models import Warehouse


def _c(phone: str) -> APIClient:
    user = User.objects.get(phone=phone)
    client = APIClient()
    client.force_authenticate(user=user)
    return client


@pytest.fixture
def phase8_setup(db):
    wh1 = Warehouse.objects.create(name="المخزن ١", name_en="WH1", code="W1", active=True)
    wh2 = Warehouse.objects.create(name="المخزن ٢", name_en="WH2", code="W2", active=True)
    tier = PriceTier.objects.create(name="جملة", name_en="Wholesale", sort_order=1)

    mgr = User.objects.create_user("07708000001", "Pass1234", name="Mgr", role=Role.MANAGER)
    acct = User.objects.create_user("07708000002", "Pass1234", name="Acct", role=Role.ACCOUNTANT)
    keeper = User.objects.create_user("07708000003", "Pass1234", name="Keeper1", role=Role.WAREHOUSE, warehouse=wh1)
    staff = User.objects.create_user("07708000004", "Pass1234", name="Staff1", role=Role.STAFF, warehouse=wh1)
    rep1 = User.objects.create_user("07708000005", "Pass1234", name="Rep1", role=Role.REP, rep_kinds=[RepKind.SALES])
    rep2 = User.objects.create_user("07708000006", "Pass1234", name="Rep2", role=Role.REP, rep_kinds=[RepKind.SALES])
    shop_u = User.objects.create_user("07708000010", "Pass1234", name="Shop", role=Role.SHOP)

    c1 = Customer.objects.create(
        name="محل الهدى",
        owner_name="Shop",
        phone="07708000010",
        city="بغداد",
        area="المنصور",
        street="1",
        landmark="A",
        tier=tier,
        rep=rep1,
        debt_limit=50000,
        status=CustomerStatus.ACTIVE,
        active=True,
    )
    ShopMembership.objects.create(user=shop_u, customer=c1, is_primary=True, is_active_shop=True)

    c2 = Customer.objects.create(
        name="محل الفرات",
        owner_name="Ali",
        phone="07708000011",
        city="بغداد",
        area="الكرادة",
        street="2",
        landmark="B",
        tier=tier,
        rep=rep2,
        debt_limit=500000,
        status=CustomerStatus.ACTIVE,
        active=True,
    )

    return {"wh1": wh1, "wh2": wh2, "tier": tier, "mgr": mgr, "acct": acct, "keeper": keeper, "staff": staff, "rep1": rep1, "rep2": rep2, "c1": c1, "c2": c2}


@pytest.mark.django_db
def test_usd_rates_commissions_month_close_and_payment_reversal_lock(phase8_setup):
    mgr = _c("07708000001")
    acct = _c("07708000002")
    keeper = _c("07708000003")
    rep1 = _c("07708000005")
    rep2 = _c("07708000006")
    shop = _c("07708000010")
    wh1, tier = phase8_setup["wh1"], phase8_setup["tier"]

    # USD rates: keeper & rep blocked from POST; accountant sets rate & notifies & syncs SystemSetting
    assert keeper.post("/api/v1/usd-rates", {"rate": 1520}, format="json").status_code == 403
    assert acct.post("/api/v1/usd-rates", {"rate": 1520}, format="json").status_code == 200
    assert SystemSetting.get_solo().usd_rate == 1520
    rates = shop.get("/api/v1/usd-rates").json()["data"]
    assert len(rates) == 1 and rates[0]["rate"] == 1520 and rates[0]["date"] == baghdad_today().isoformat()

    # Stock item and create two invoices (one for rep1, one for rep2)
    exp = (baghdad_today() + timedelta(days=90)).isoformat()
    it = mgr.post(
        "/api/v1/items",
        {"name": "تمر", "name_en": "Dates", "category": "تمور", "commission_percent": 2, "pieces_per_carton": 10, "prices": [{"tier_id": tier.id, "carton_price": 100000, "piece_price": 10000}]},
        format="json",
    ).json()["data"]["id"]
    keeper.post("/api/v1/receipts", {"warehouse_id": wh1.id, "lines": [{"item_id": it, "unit": "piece", "qty": 50, "expiry_date": exp, "purchase_price": 6000}]}, format="json")

    def _inv(cust, rep_u, qty, suffix):
        tot = qty * 10000
        o = Order.objects.create(number=f"O-{suffix}", customer=cust, warehouse=wh1, rep=rep_u, created_by=rep_u, source=OrderSource.REP, status=OrderStatus.CONFIRMED, subtotal=tot, total=tot)
        ol = OrderLine.objects.create(order=o, item_id=it, warehouse=wh1, unit="piece", qty=qty, pieces=qty, unit_price=10000, total=tot)
        with transaction.atomic():
            return create_invoice_for_order(
                order=o,
                delivered_line_specs=[{"warehouse_id": wh1.id, "item_id": it, "unit": "piece", "qty": qty, "pieces": qty, "unit_price": 10000, "total": tot, "order_line": ol}],
                delivery_rep=rep_u,
                actor=phase8_setup["mgr"],
            )

    inv1 = _inv(phase8_setup["c1"], phase8_setup["rep1"], 10, "C1")  # 100,000
    _inv(phase8_setup["c2"], phase8_setup["rep2"], 5, "C2")  # 50,000

    # Collect 100,000 from c1 and 50,000 from c2, plus 20,000 good return on c1 -> net 80,000 for rep1
    acct.post("/api/v1/payments", {"customer_id": phase8_setup["c1"].id, "amount": 100000, "method": "cash"}, format="json")
    acct.post("/api/v1/payments", {"customer_id": phase8_setup["c2"].id, "amount": 50000, "method": "cash"}, format="json")
    mgr.post("/api/v1/returns", {"invoice_id": inv1.id, "lines": [{"item_id": it, "unit": "piece", "qty": 2, "condition": "good"}]}, format="json")

    month = baghdad_today().strftime("%Y-%m")
    # Rep1 sees only self; commission = 2% of 100,000 = 2,000, returns_deducted = 2% of 20,000 = 400 -> net 1,600
    r1_rows = rep1.get(f"/api/v1/commissions?month={month}").json()["data"]
    assert len(r1_rows) == 1 and r1_rows[0]["rep"]["id"] == phase8_setup["rep1"].id
    assert r1_rows[0]["paid_sales"] == 100000 and r1_rows[0]["commission"] == 2000
    assert r1_rows[0]["returns_deducted"] == 400 and r1_rows[0]["net"] == 1600
    assert keeper.get(f"/api/v1/commissions?month={month}").status_code == 403

    lines_mgr = mgr.get(f"/api/v1/commissions/lines?month={month}").json()["data"]
    assert len(lines_mgr) == 2 and all(ln["rate"] == 2 for ln in lines_mgr)

    # Current or future month cannot be closed prematurely
    assert acct.post("/api/v1/commissions/close", {"month": month}, format="json").status_code == 403
    assert mgr.post("/api/v1/commissions/close", {"month": month}, format="json").status_code == 409

    # Backdate an invoice & payment into previous month, close previous month, then test MONTH_CLOSED reversal lock
    prev_day = baghdad_today().replace(day=1) - timedelta(days=2)
    prev_month = prev_day.strftime("%Y-%m")
    _inv(phase8_setup["c2"], phase8_setup["rep2"], 5, "PREV")  # 50,000
    prev_p = acct.post("/api/v1/payments", {"customer_id": phase8_setup["c2"].id, "amount": 50000, "method": "cash"}, format="json").json()["data"]
    prev_dt = datetime.combine(prev_day, datetime.min.time(), tzinfo=UTC) + timedelta(hours=12)
    Payment.objects.filter(id=prev_p["id"]).update(created_at=prev_dt)

    closed = mgr.post("/api/v1/commissions/close", {"month": prev_month}, format="json")
    assert closed.status_code == 200
    closed_rows = closed.json()["data"]
    rep2_closed = [r for r in closed_rows if r["rep"]["id"] == phase8_setup["rep2"].id][0]
    assert rep2_closed["net"] == 1000 and rep2_closed["closed"] is True

    # Reversing payment from a closed month is blocked with MONTH_CLOSED (409)!
    rev_res = acct.post(f"/api/v1/payments/{prev_p['id']}/reverse", {"reason": "Late edit"}, format="json")
    assert rev_res.status_code == 409 and rev_res.json()["code"] == "MONTH_CLOSED"
    assert rep2.get("/api/v1/commissions/closed").json()["data"] == [prev_month]


@pytest.mark.django_db
def test_dashboard_and_reports_aggregation_and_role_redactions(phase8_setup):
    mgr = _c("07708000001")
    acct = _c("07708000002")
    keeper = _c("07708000003")
    staff = _c("07708000004")
    rep1 = _c("07708000005")
    shop = _c("07708000010")
    wh1, wh2, tier = phase8_setup["wh1"], phase8_setup["wh2"], phase8_setup["tier"]

    near_exp = (baghdad_today() + timedelta(days=10)).isoformat()
    far_exp = (baghdad_today() + timedelta(days=180)).isoformat()

    it = mgr.post(
        "/api/v1/items",
        {"name": "قهوة", "name_en": "Coffee", "category": "مشروبات", "min_stock": 20, "pieces_per_carton": 10, "prices": [{"tier_id": tier.id, "carton_price": 100000, "piece_price": 10000}]},
        format="json",
    ).json()["data"]["id"]
    # Receipt in WH1 (near expiry, low stock after sale) and WH2 (unused/slow)
    keeper.post("/api/v1/receipts", {"warehouse_id": wh1.id, "lines": [{"item_id": it, "unit": "piece", "qty": 15, "expiry_date": near_exp, "purchase_price": 6000}]}, format="json")
    mgr.post("/api/v1/receipts", {"warehouse_id": wh2.id, "lines": [{"item_id": it, "unit": "piece", "qty": 25, "expiry_date": far_exp, "purchase_price": 6000}]}, format="json")

    # Invoice 10 pieces (100,000 IQD, cost 60,000 -> profit 40,000; over c1 debt_limit 50,000!)
    o = Order.objects.create(number="O-RPT", customer=phase8_setup["c1"], warehouse=wh1, rep=phase8_setup["rep1"], created_by=phase8_setup["rep1"], source=OrderSource.REP, status=OrderStatus.CONFIRMED, subtotal=100000, total=100000)
    ol = OrderLine.objects.create(order=o, item_id=it, warehouse=wh1, unit="piece", qty=10, pieces=10, unit_price=10000, total=100000)
    with transaction.atomic():
        create_invoice_for_order(
            order=o,
            delivered_line_specs=[{"warehouse_id": wh1.id, "item_id": it, "unit": "piece", "qty": 10, "pieces": 10, "unit_price": 10000, "total": 100000, "order_line": ol}],
            delivery_rep=phase8_setup["rep1"],
            actor=phase8_setup["mgr"],
        )

    # Dashboard redaction checks
    assert shop.get("/api/v1/dashboard").status_code == 403
    d_mgr = mgr.get("/api/v1/dashboard").json()["data"]
    assert d_mgr["month_sales"] == 100000 and d_mgr["month_profit"] == 40000 and d_mgr["total_debt"] == 100000 and d_mgr["over_limit_customers"] == 1 and d_mgr["near_expiry"] == 1
    assert len(d_mgr["last7"]) == 7
    d_keeper = keeper.get("/api/v1/dashboard").json()["data"]
    assert "month_profit" not in d_keeper and d_keeper["total_debt"] == 0 and d_keeper["today_collected"] == 0
    d_rep = rep1.get("/api/v1/dashboard").json()["data"]
    assert "month_profit" not in d_rep and d_rep["total_debt"] == 100000

    # Daily sales redactions
    daily_mgr = acct.get("/api/v1/reports/daily-sales").json()["data"]
    assert daily_mgr["sales"] == 100000 and daily_mgr["by_warehouse"][0]["profit"] == 40000
    daily_staff = staff.get("/api/v1/reports/daily-sales").json()["data"]
    assert "profit" not in daily_staff["by_warehouse"][0] and daily_staff["collected"] == 0

    # Sales grouped by item/customer/rep/warehouse
    assert staff.get("/api/v1/reports/sales").status_code == 403
    sales_rep = rep1.get("/api/v1/reports/sales?group_by=customer").json()["data"]
    assert len(sales_rep) == 1 and sales_rep[0]["key"] and "profit" not in sales_rep[0]

    # Slow moving scoped to WH2 returns item with value visible to mgr but hidden from staff (when queried on WH1)
    slow_wh2 = mgr.get(f"/api/v1/reports/slow-moving?days=30&warehouse_id={wh2.id}").json()["data"]
    assert len(slow_wh2) == 1 and slow_wh2[0]["stock"] == 25 and slow_wh2[0]["item_id"] == it
    near_staff = staff.get("/api/v1/reports/near-expiry?days=30").json()["data"]
    assert len(near_staff) == 1 and near_staff[0]["batch"]["warehouse_id"] == wh1.id and "purchase_price" not in near_staff[0]["batch"]

    # Debts report: keeper blocked; rep1 sees c1 over_limit=True
    assert keeper.get("/api/v1/reports/debts").status_code == 403
    debts = rep1.get("/api/v1/reports/debts").json()["data"]
    assert len(debts) == 1 and debts[0]["customer"]["id"] == phase8_setup["c1"].id and debts[0]["customer"]["balance"] == 100000 and debts[0]["open_invoices"] == 1
