"""Phase 5 tests: Customers, multi-shop switching, SpecialPrices, Orders, Offline sync, Approvals, Multi-warehouse prep."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from rest_framework.test import APIClient

from apps.accounts.models import PriceTier, Role, SystemSetting, User
from apps.catalog.models import PriceChange
from apps.common.utils import baghdad_today
from apps.customers.models import Customer, CustomerStatus, ShopMembership
from apps.orders.models import Order
from apps.warehouses.models import Warehouse


@pytest.fixture
def phase5_setup(db):
    wh1 = Warehouse.objects.create(name="الصناعة", name_en="Sinaa", code="W1")
    wh2 = Warehouse.objects.create(name="الحر", name_en="Hur", code="W2")
    tier1 = PriceTier.objects.create(name="جملة", name_en="Wholesale", sort_order=1)
    tier2 = PriceTier.objects.create(name="تجزئة", name_en="Retail", sort_order=2)
    SystemSetting.get_solo()

    mgr = User.objects.create_user(phone="07704000001", password="Password123", name="Mgr", role=Role.MANAGER)
    acct = User.objects.create_user(phone="07704000002", password="Password123", name="Acct", role=Role.ACCOUNTANT)
    k1 = User.objects.create_user(phone="07704000003", password="Password123", name="Keeper1", role=Role.WAREHOUSE, warehouse=wh1)
    k2 = User.objects.create_user(phone="07704000004", password="Password123", name="Keeper2", role=Role.WAREHOUSE, warehouse=wh2)
    sales_rep = User.objects.create_user(
        phone="07704000005",
        password="Password123",
        name="SalesRep",
        role=Role.REP,
        rep_kinds=["sales", "delivery"],
        rep_categories=["زيوت"],
    )
    del_only_rep = User.objects.create_user(
        phone="07704000006", password="Password123", name="Driver", role=Role.REP, rep_kinds=["delivery"]
    )

    cust = Customer.objects.create(
        name="سوبرماركت النور",
        owner_name="علي",
        phone="07704000007",
        city="بغداد",
        area="المنصور",
        street="14 رمضان",
        landmark="قرب الساعة",
        tier=tier1,
        rep=sales_rep,
        debt_limit=200_000,
        status=CustomerStatus.ACTIVE,
        active=True,
    )
    shop_user = User.objects.create_user(phone="07704000007", password="Password123", name="علي", role=Role.SHOP)
    ShopMembership.objects.create(user=shop_user, customer=cust, is_primary=True, is_active_shop=True)

    return {
        "wh1": wh1,
        "wh2": wh2,
        "tier1": tier1,
        "tier2": tier2,
        "mgr": mgr,
        "acct": acct,
        "k1": k1,
        "k2": k2,
        "sales_rep": sales_rep,
        "del_only_rep": del_only_rep,
        "cust": cust,
        "shop_user": shop_user,
    }


def _c(phone: str) -> APIClient:
    c = APIClient()
    tok = c.post("/api/v1/auth/login", {"phone": phone, "password": "Password123"}, format="json").json()["data"]["token"]
    c.credentials(HTTP_AUTHORIZATION=f"Bearer {tok}")
    return c


@pytest.mark.django_db
def test_multishop_special_price_and_customer_approval(phase5_setup):
    mgr = _c("07704000001")
    k1 = _c("07704000003")
    shop = _c("07704000007")
    cust = phase5_setup["cust"]
    tier1 = phase5_setup["tier1"]

    # Create item
    item_id = mgr.post(
        "/api/v1/items",
        {
            "name": "زيت زيتون",
            "name_en": "Olive Oil",
            "category": "زيوت",
            "pieces_per_carton": 10,
            "prices": [{"tier_id": tier1.id, "carton_price": 20000, "piece_price": 2100}],
        },
        format="json",
    ).json()["data"]["id"]

    # Special price overrides tier price for shop
    sp_res = mgr.post(f"/api/v1/customers/{cust.id}/prices", {"item_id": item_id, "prices": {"carton_price": 18500, "piece_price": 1900}}, format="json")
    assert sp_res.status_code == 200
    assert shop.get(f"/api/v1/items/{item_id}").json()["data"]["prices"][0]["carton_price"] == 18500

    # Keeper cannot see customer debt balance/limit (omitted so UI treats them as undefined)
    k1_cust = k1.get(f"/api/v1/customers/{cust.id}").json()["data"]
    assert "balance" not in k1_cust and "debt_limit" not in k1_cust

    # Shop adds a branch and switches active shop
    branch = shop.post(
        "/api/v1/me/shops",
        {"shop_name": "فرع الكرادة", "city": "بغداد", "area": "الكرادة", "street": "62", "landmark": "الواثق"},
        format="json",
    )
    assert branch.status_code == 201
    bid = branch.json()["data"]["customer_id"]
    assert len(shop.get("/api/v1/me/shops").json()["data"]) == 2
    sw = shop.post("/api/v1/me/shop", {"customer_id": bid}, format="json")
    assert sw.status_code == 200
    assert sw.json()["data"]["customer_id"] == bid


@pytest.mark.django_db
def test_order_rbac_discount_approval_and_multi_warehouse_prepare_pin_privacy(phase5_setup):
    mgr = _c("07704000001")
    acct = _c("07704000002")
    k1 = _c("07704000003")
    k2 = _c("07704000004")
    rep = _c("07704000005")
    driver = _c("07704000006")
    shop = _c("07704000007")

    wh1, wh2, tier1, cust = phase5_setup["wh1"], phase5_setup["wh2"], phase5_setup["tier1"], phase5_setup["cust"]
    exp = (baghdad_today() + timedelta(days=90)).isoformat()

    it_oil = mgr.post(
        "/api/v1/items",
        {"name": "زيت", "name_en": "Oil", "category": "زيوت", "pieces_per_carton": 10, "prices": [{"tier_id": tier1.id, "carton_price": 20000, "piece_price": 2000}]},
        format="json",
    ).json()["data"]["id"]
    it_rice = mgr.post(
        "/api/v1/items",
        {"name": "رز", "name_en": "Rice", "category": "حبوب", "pieces_per_carton": 10, "prices": [{"tier_id": tier1.id, "carton_price": 30000, "piece_price": 3000}]},
        format="json",
    ).json()["data"]["id"]

    # Stock Oil in WH1 and Rice in WH2
    k1.post("/api/v1/receipts", {"warehouse_id": wh1.id, "lines": [{"item_id": it_oil, "unit": "carton", "qty": 10, "expiry_date": exp, "purchase_price": 1500}]}, format="json")
    k2.post("/api/v1/receipts", {"warehouse_id": wh2.id, "lines": [{"item_id": it_rice, "unit": "carton", "qty": 10, "expiry_date": exp, "purchase_price": 2200}]}, format="json")

    # Delivery-only rep and rep selling unauthorized category ("حبوب") blocked
    assert driver.post("/api/v1/orders", {"customer_id": cust.id, "warehouse_id": wh1.id, "lines": [{"item_id": it_oil, "unit": "carton", "qty": 1}]}, format="json").status_code == 403
    assert rep.post("/api/v1/orders", {"customer_id": cust.id, "warehouse_id": wh2.id, "lines": [{"item_id": it_rice, "unit": "carton", "qty": 1}]}, format="json").status_code == 403

    # Shop cannot order > 100 units
    assert shop.post("/api/v1/orders", {"warehouse_id": wh1.id, "lines": [{"item_id": it_oil, "unit": "carton", "qty": 101}]}, format="json").status_code == 400

    # Rep submits order with 5% discount (> 3% max) -> needs_approval ("discount")
    o_res = rep.post(
        "/api/v1/orders",
        {"customer_id": cust.id, "warehouse_id": wh1.id, "discount_percent": 5, "lines": [{"item_id": it_oil, "unit": "carton", "qty": 2}]},
        format="json",
    )
    assert o_res.status_code == 201
    oid = o_res.json()["data"]["id"]
    assert o_res.json()["data"]["status"] == "needs_approval"
    assert "discount" in o_res.json()["data"]["approval_for"]

    # Accountant cannot approve discount (>3%); Manager approves
    assert acct.post(f"/api/v1/orders/{oid}/approve", format="json").status_code == 403
    app_res = mgr.post(f"/api/v1/orders/{oid}/approve", format="json")
    assert app_res.status_code == 200
    assert app_res.json()["data"]["status"] == "approved"

    # Manager creates a mixed order requiring WH1 (Oil) + WH2 (Rice)
    mixed = mgr.post(
        "/api/v1/orders",
        {
            "customer_id": cust.id,
            "warehouse_id": wh1.id,
            "lines": [
                {"item_id": it_oil, "unit": "carton", "qty": 1},
                {"item_id": it_rice, "unit": "carton", "qty": 1},
            ],
        },
        format="json",
    )
    # Wait: Oil is in WH1, Rice is ONLY in WH2 -> standard single-WH stock check catches shortage unless rice is stocked or placed from WH2; stock Rice in WH1=1, or check WH2 prep split
    assert mixed.status_code == 409  # OUT_OF_STOCK cleanly on initial WH1 check!

    # Also stock 2 cartons Rice in WH1 so order passes initial check, then prepare Rice from WH2!
    k1.post("/api/v1/receipts", {"warehouse_id": wh1.id, "lines": [{"item_id": it_rice, "unit": "carton", "qty": 2, "expiry_date": exp, "purchase_price": 2200}]}, format="json")
    mixed = mgr.post(
        "/api/v1/orders",
        {
            "customer_id": cust.id,
            "warehouse_id": wh1.id,
            "lines": [
                {"item_id": it_oil, "unit": "carton", "qty": 1},
                {"item_id": it_rice, "unit": "carton", "qty": 1},
            ],
        },
        format="json",
    )
    assert mixed.status_code == 201
    assert "prep_parts" not in mixed.json()["data"]
    mid = mixed.json()["data"]["id"]
    plan = k1.get(f"/api/v1/orders/{mid}/prep-plan").json()["data"]
    l1_id = plan[0]["line_id"]
    l2_id = plan[1]["line_id"]

    # Keeper1 assigns line1->WH1 and line2->WH2 and prepares WH1 part -> delivery NOT generated yet!
    p1 = k1.post(f"/api/v1/orders/{mid}/prepare", {"warehouses": {str(l1_id): wh1.id, str(l2_id): wh2.id}}, format="json")
    assert p1.status_code == 200
    assert p1.json()["data"]["delivery"] is None

    # Keeper2 completes WH2 part -> delivery created!
    p2 = k2.post(f"/api/v1/orders/{mid}/prepare", {"warehouses": {}}, format="json")
    assert p2.status_code == 200
    assert p2.json()["data"]["delivery"] is not None
    # Confidential rule: PIN must be None for keeper, manager, rep, and visible ONLY to owning shop!
    assert p2.json()["data"]["delivery"]["pin"] is None
    assert mgr.get(f"/api/v1/orders/{mid}").json()["data"]["delivery"]["pin"] is None
    assert rep.get(f"/api/v1/orders/{mid}").json()["data"]["delivery"]["pin"] is None
    shop_order = shop.get(f"/api/v1/orders/{mid}").json()["data"]
    assert shop_order["delivery"]["pin"] is not None and len(shop_order["delivery"]["pin"]) == 4


@pytest.mark.django_db
def test_offline_order_3day_price_history_partial_split_and_idempotency(phase5_setup):
    mgr = _c("07704000001")
    k1 = _c("07704000003")
    rep = _c("07704000005")
    wh1, tier1, cust = phase5_setup["wh1"], phase5_setup["tier1"], phase5_setup["cust"]

    item_id = mgr.post(
        "/api/v1/items",
        {"name": "زيت دوار", "name_en": "Sunflower Oil", "category": "زيوت", "pieces_per_carton": 10, "prices": [{"tier_id": tier1.id, "carton_price": 20000, "piece_price": 2000}]},
        format="json",
    ).json()["data"]["id"]

    # Backdate initial PriceChange to 2 days ago and update current price higher
    two_days_ago = datetime.now(tz=UTC) - timedelta(days=2)
    PriceChange.objects.filter(item_id=item_id).update(effective_from=two_days_ago - timedelta(hours=1))
    mgr.patch(f"/api/v1/items/{item_id}", {"prices": [{"tier_id": tier1.id, "carton_price": 25000, "piece_price": 2500}]}, format="json")

    exp = (baghdad_today() + timedelta(days=60)).isoformat()
    # Only 3 cartons (30 pieces) on shelf
    k1.post("/api/v1/receipts", {"warehouse_id": wh1.id, "lines": [{"item_id": item_id, "unit": "carton", "qty": 3, "expiry_date": exp, "purchase_price": 1500}]}, format="json")

    # Rep submits offline order recorded 2 days ago for 5 cartons -> splits 3 active + 2 waiting_stock, using historical 20000 price!
    payload = {
        "customer_id": cust.id,
        "warehouse_id": wh1.id,
        "offline": True,
        "recorded_at": two_days_ago.isoformat(),
        "client_ref": "offline-uuid-9001",
        "lines": [{"item_id": item_id, "unit": "carton", "qty": 5}],
    }
    res1 = rep.post("/api/v1/orders", payload, format="json", HTTP_IDEMPOTENCY_KEY="idem-ord-9001")
    assert res1.status_code == 201, res1.json()
    d = res1.json()["data"]
    assert d["total"] == 3 * 20000
    assert d["rest_order_id"] is not None
    child = Order.objects.get(id=d["rest_order_id"])
    assert child.waiting_stock is True
    assert child.status == "expired"
    assert child.total == 2 * 20000

    # Replay via same client_ref or Idempotency-Key returns same order without duplicate split
    res2 = rep.post("/api/v1/orders", payload, format="json", HTTP_IDEMPOTENCY_KEY="idem-ord-9001")
    assert res2.status_code == 201
    assert res2.json()["data"]["id"] == d["id"]
    assert Order.objects.filter(parent_id=d["id"]).count() == 1


@pytest.mark.django_db
def test_active_customer_orders_auto_approve_and_only_over_debt_requires_approval(phase5_setup):
    mgr = _c("07704000001")
    acct = _c("07704000002")
    k1 = _c("07704000003")
    rep = _c("07704000005")
    shop = _c("07704000007")
    wh1, tier1, cust = phase5_setup["wh1"], phase5_setup["tier1"], phase5_setup["cust"]
    # debt_limit = 200_000, balance = 0
    exp = (baghdad_today() + timedelta(days=90)).isoformat()
    it_oil = mgr.post(
        "/api/v1/items",
        {"name": "زيت نظيف", "name_en": "Clean Oil", "category": "زيوت", "pieces_per_carton": 10, "prices": [{"tier_id": tier1.id, "carton_price": 50000, "piece_price": 5000}]},
        format="json",
    ).json()["data"]["id"]
    k1.post("/api/v1/receipts", {"warehouse_id": wh1.id, "lines": [{"item_id": it_oil, "unit": "carton", "qty": 20, "expiry_date": exp, "purchase_price": 3000}]}, format="json")

    # 1. Shop places 150,000 order (<= 200,000 debt_limit) -> approved immediately & keeper can prepare directly!
    s_ok = shop.post("/api/v1/orders", {"warehouse_id": wh1.id, "lines": [{"item_id": it_oil, "unit": "carton", "qty": 3}]}, format="json")
    assert s_ok.status_code == 201, s_ok.json()
    s_data = s_ok.json()["data"]
    assert s_data["status"] == "approved"
    assert s_data["approval_for"] == []
    assert k1.post(f"/api/v1/orders/{s_data['id']}/prepare", {"warehouses": {str(it_oil): wh1.id}}, format="json").status_code == 200

    # 2. Rep places 50,000 order (150k committed + 50k = 200,000 <= 200,000) -> approved immediately!
    r_ok = rep.post("/api/v1/orders", {"customer_id": cust.id, "warehouse_id": wh1.id, "lines": [{"item_id": it_oil, "unit": "carton", "qty": 1}]}, format="json")
    assert r_ok.status_code == 201
    assert r_ok.json()["data"]["status"] == "approved"

    # 3. Shop places another 50,000 order (200k committed + 50k = 250,000 > 200,000) -> needs_approval ("debt")!
    s_over = shop.post("/api/v1/orders", {"warehouse_id": wh1.id, "lines": [{"item_id": it_oil, "unit": "carton", "qty": 1}]}, format="json")
    assert s_over.status_code == 201
    over_id = s_over.json()["data"]["id"]
    assert s_over.json()["data"]["status"] == "needs_approval"
    assert s_over.json()["data"]["approval_for"] == ["debt"]
    # Warehouse keeper cannot prepare an unapproved over-debt order
    assert k1.post(f"/api/v1/orders/{over_id}/prepare", {"warehouses": {str(it_oil): wh1.id}}, format="json").status_code == 400
    # Accountant can approve debt-only order, then keeper prepares!
    assert acct.post(f"/api/v1/orders/{over_id}/approve", {}, format="json").status_code == 200
    assert k1.post(f"/api/v1/orders/{over_id}/prepare", {"warehouses": {str(it_oil): wh1.id}}, format="json").status_code == 200


@pytest.mark.django_db
def test_keeper_carrier_pick_delivery_carrier_and_shipments_driver_id(phase5_setup):
    from apps.deliveries.models import Vehicle
    from apps.notifications.models import Notification
    from apps.accounts.models import RepKind

    mgr = _c("07704000001")
    k1 = _c("07704000003")
    k2 = _c("07704000004")
    wh1, wh2, tier1, cust = phase5_setup["wh1"], phase5_setup["wh2"], phase5_setup["tier1"], phase5_setup["cust"]

    drv1 = User.objects.create_user(
        phone="07704000091",
        password="Password123",
        name="Driver One",
        role=Role.REP,
        rep_kinds=[RepKind.DELIVERY],
    )
    drv2 = User.objects.create_user(
        phone="07704000092",
        password="Password123",
        name="Driver Two",
        role=Role.REP,
        rep_kinds=[RepKind.DELIVERY],
    )
    sales_only = User.objects.create_user(
        phone="07704000093",
        password="Password123",
        name="Sales Only",
        role=Role.REP,
        rep_kinds=[RepKind.SALES],
    )
    veh1 = Vehicle.objects.create(name="كيا بونكو 1", plate="11111", capacity_kg=1000, driver=drv1, active=True)
    veh2 = Vehicle.objects.create(name="إيسوزو 2", plate="22222", capacity_kg=2000, driver=drv2, active=True)

    exp = (baghdad_today() + timedelta(days=90)).isoformat()
    it_a = mgr.post(
        "/api/v1/items",
        {"name": "رز أبيض", "name_en": "White Rice", "category": "حبوب", "pieces_per_carton": 10, "weight_kg": 1, "prices": [{"tier_id": tier1.id, "carton_price": 10000, "piece_price": 1000}]},
        format="json",
    ).json()["data"]["id"]
    it_b = mgr.post(
        "/api/v1/items",
        {"name": "زيت طعام", "name_en": "Cooking Oil", "category": "زيوت", "pieces_per_carton": 10, "weight_kg": 1, "prices": [{"tier_id": tier1.id, "carton_price": 12000, "piece_price": 1200}]},
        format="json",
    ).json()["data"]["id"]
    k1.post("/api/v1/receipts", {"warehouse_id": wh1.id, "lines": [{"item_id": it_a, "unit": "carton", "qty": 20, "expiry_date": exp, "purchase_price": 600}]}, format="json")
    k1.post("/api/v1/receipts", {"warehouse_id": wh1.id, "lines": [{"item_id": it_b, "unit": "carton", "qty": 10, "expiry_date": exp, "purchase_price": 800}]}, format="json")
    k2.post("/api/v1/receipts", {"warehouse_id": wh2.id, "lines": [{"item_id": it_b, "unit": "carton", "qty": 20, "expiry_date": exp, "purchase_price": 800}]}, format="json")

    # 1. Invalid rep_id on prepare -> 422
    o1 = mgr.post(
        "/api/v1/orders",
        {"customer_id": cust.id, "warehouse_id": wh1.id, "lines": [{"item_id": it_a, "unit": "carton", "qty": 2}]},
        format="json",
    ).json()["data"]
    bad_rep = k1.post(
        f"/api/v1/orders/{o1['id']}/prepare",
        {"warehouses": {str(it_a): wh1.id}, "rep_id": sales_only.id},
        format="json",
    )
    assert bad_rep.status_code == 422

    # 2. Two-warehouse prepare: k1 chooses drv1 + veh2 -> stored on carrier_pick, applied & cleared when k2 finishes
    o_split = mgr.post(
        "/api/v1/orders",
        {
            "customer_id": cust.id,
            "warehouse_id": wh1.id,
            "lines": [
                {"item_id": it_a, "unit": "carton", "qty": 2},
                {"item_id": it_b, "unit": "carton", "qty": 2},
            ],
        },
        format="json",
    ).json()["data"]
    p1 = k1.post(
        f"/api/v1/orders/{o_split['id']}/prepare",
        {"warehouses": {str(it_a): wh1.id, str(it_b): wh2.id}, "rep_id": drv1.id, "vehicle_id": veh2.id},
        format="json",
    )
    assert p1.status_code == 200, p1.json()
    assert p1.json()["data"]["carrier_pick"] == {"rep_id": drv1.id, "vehicle_id": veh2.id}

    p2 = k2.post(f"/api/v1/orders/{o_split['id']}/prepare", {"warehouses": {}}, format="json")
    assert p2.status_code == 200, p2.json()
    p2_data = p2.json()["data"]
    assert "carrier_pick" not in p2_data
    assert p2_data["delivery"]["rep"]["id"] == drv1.id
    assert p2_data["delivery"]["vehicle"]["id"] == veh2.id
    assert Notification.objects.filter(user=drv1, ref_type="order", ref_id=str(o_split["id"])).exists()

    # 3. Single-warehouse prepare with vehicle_id only -> gets vehicle's own driver
    prep_v = k1.post(
        f"/api/v1/orders/{o1['id']}/prepare",
        {"warehouses": {str(it_a): wh1.id}, "vehicle_id": veh1.id},
        format="json",
    )
    assert prep_v.status_code == 200
    assert prep_v.json()["data"]["delivery"]["rep"]["id"] == drv1.id
    assert prep_v.json()["data"]["delivery"]["vehicle"]["id"] == veh1.id

    # 4. delivery/carrier changes rep & removes vehicle (vehicle_id: null) until loaded; k2 forbidden
    assert k2.post(
        f"/api/v1/orders/{o1['id']}/delivery/carrier",
        {"rep_id": drv2.id, "vehicle_id": None},
        format="json",
    ).status_code == 403
    chg = k1.post(
        f"/api/v1/orders/{o1['id']}/delivery/carrier",
        {"rep_id": drv2.id, "vehicle_id": None},
        format="json",
    )
    assert chg.status_code == 200, chg.json()
    assert chg.json()["data"]["delivery"]["rep"]["id"] == drv2.id
    assert chg.json()["data"]["delivery"].get("vehicle") is None
    assert Notification.objects.filter(user=drv2, ref_type="order", ref_id=str(o1["id"])).exists()

    # 5. shipments with per-load driver_id override on split vehicles
    o_multi = mgr.post(
        "/api/v1/orders",
        {"customer_id": cust.id, "warehouse_id": wh1.id, "lines": [{"item_id": it_a, "unit": "carton", "qty": 4}]},
        format="json",
    ).json()["data"]
    assert k1.post(f"/api/v1/orders/{o_multi['id']}/prepare", {"warehouses": {str(it_a): wh1.id}}, format="json").status_code == 200
    split_res = k1.post(
        f"/api/v1/orders/{o_multi['id']}/shipments",
        {
            "loads": [
                {"vehicle_id": veh1.id, "driver_id": drv2.id, "lines": [{"item_id": it_a, "unit": "carton", "qty": 2}]},
                {"vehicle_id": veh2.id, "driver_id": drv1.id, "lines": [{"item_id": it_a, "unit": "carton", "qty": 2}]},
            ]
        },
        format="json",
    )
    assert split_res.status_code == 200, split_res.json()
    child_ids = split_res.json()["data"]["shipments"]
    c1 = mgr.get(f"/api/v1/orders/{child_ids[0]}").json()["data"]
    c2 = mgr.get(f"/api/v1/orders/{child_ids[1]}").json()["data"]
    assert c1["delivery"]["vehicle"]["id"] == veh1.id and c1["delivery"]["rep"]["id"] == drv2.id
    assert c2["delivery"]["vehicle"]["id"] == veh2.id and c2["delivery"]["rep"]["id"] == drv1.id

    # 6. Shop/unassigned-rep order falls back `rep_id` cleanly to assigned delivery rep on detail & list
    from apps.customers.models import Customer, CustomerStatus

    no_rep_cust = Customer.objects.create(
        name="محل بلا مندوب",
        owner_name="صاحب",
        phone="07704000099",
        area="حي الحسين",
        tier=tier1,
        rep=None,
        debt_limit=500_000,
        status=CustomerStatus.ACTIVE,
        active=True,
    )
    o_shop = mgr.post(
        "/api/v1/orders",
        {"customer_id": no_rep_cust.id, "warehouse_id": wh1.id, "lines": [{"item_id": it_a, "unit": "carton", "qty": 1}]},
        format="json",
    ).json()["data"]
    assert o_shop["rep_id"] == 0
    prep_no_rep = k1.post(
        f"/api/v1/orders/{o_shop['id']}/prepare",
        {"warehouses": {str(it_a): wh1.id}, "rep_id": drv1.id},
        format="json",
    )
    assert prep_no_rep.status_code == 200
    assert prep_no_rep.json()["data"]["rep_id"] == drv1.id
    assert prep_no_rep.json()["data"]["delivery"]["rep"]["id"] == drv1.id
    chg_no_rep = k1.post(
        f"/api/v1/orders/{o_shop['id']}/delivery/carrier",
        {"rep_id": drv2.id, "vehicle_id": None},
        format="json",
    )
    assert chg_no_rep.status_code == 200
    assert chg_no_rep.json()["data"]["rep_id"] == drv2.id
    listed = {x["id"]: x for x in mgr.get("/api/v1/orders").json()["data"]}
    assert listed[o_shop["id"]]["rep_id"] == drv2.id

    # Split parent cannot use delivery/carrier (409)
    assert k1.post(
        f"/api/v1/orders/{o_multi['id']}/delivery/carrier",
        {"rep_id": drv1.id, "vehicle_id": None},
        format="json",
    ).status_code == 409
