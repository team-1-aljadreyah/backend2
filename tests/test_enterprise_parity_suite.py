"""Comprehensive integration tests for all extended Al-Nahrain ERP features and frontend parity."""
from __future__ import annotations

import base64
import io
from datetime import UTC, datetime, timedelta

import pytest
from channels.testing import WebsocketCommunicator
from django.utils import timezone
from PIL import Image
from rest_framework.test import APIClient

from apps.accounts.models import PriceTier, Role, SystemSetting, User
from apps.common.utils import baghdad_today
from apps.customers.models import Customer, CustomerStatus, ShopMembership
from apps.warehouses.models import Warehouse
from config.asgi import application


def _png_data_url(color: tuple[int, int, int] = (20, 110, 180)) -> str:
    buf = io.BytesIO()
    Image.new("RGB", (24, 24), color=color).save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/png;base64,{b64}"


def _auth_client(phone: str, password: str = "StrongPass!2026") -> tuple[APIClient, str, dict]:
    cli = APIClient()
    res = cli.post("/api/v1/auth/login", {"phone": phone, "password": password}, format="json")
    assert res.status_code == 200, res.content
    token = res.json()["data"]["token"]
    user = res.json()["data"]["user"]
    cli.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
    return cli, token, user


@pytest.fixture
def erp_seed():
    SystemSetting.get_solo()
    tier = PriceTier.objects.create(name="جملة", name_en="Wholesale", sort_order=1)
    wh1 = Warehouse.objects.create(
        name="مخزن الكرخ",
        name_en="Karkh WH",
        code="WH-T1",
        capacity=2000,
        active=True,
    )
    wh2 = Warehouse.objects.create(
        name="مخزن الرصافة",
        name_en="Rusafa WH",
        code="WH-T2",
        capacity=1500,
        active=True,
    )
    mgr = User.objects.create_user(
        phone="07750000001", password="StrongPass!2026", name="المدير", role=Role.MANAGER
    )
    acct = User.objects.create_user(
        phone="07750000002", password="StrongPass!2026", name="المحاسبة", role=Role.ACCOUNTANT
    )
    keeper1 = User.objects.create_user(
        phone="07750000003", password="StrongPass!2026", name="أمين 1", role=Role.WAREHOUSE, warehouse=wh1
    )
    keeper2 = User.objects.create_user(
        phone="07750000004", password="StrongPass!2026", name="أمين 2", role=Role.WAREHOUSE, warehouse=wh2
    )
    staff1 = User.objects.create_user(
        phone="07750000005", password="StrongPass!2026", name="موظف 1", role=Role.STAFF, warehouse=wh1
    )
    rep1 = User.objects.create_user(
        phone="07750000006",
        password="StrongPass!2026",
        name="حسين المندوب",
        role=Role.REP,
        rep_kinds=["sales", "delivery"],
        rep_categories=["زيوت", "حبوب"],
        sales_target=2_000_000,
    )
    rep2 = User.objects.create_user(
        phone="07750000007",
        password="StrongPass!2026",
        name="زيد السائق",
        role=Role.REP,
        rep_kinds=["delivery"],
    )
    shop_user = User.objects.create_user(
        phone="07750000008", password="StrongPass!2026", name="أبو محمد", role=Role.SHOP
    )
    cust1 = Customer.objects.create(
        name="سوق المنصور",
        owner_name="أبو محمد",
        phone="07750000008",
        city="بغداد",
        area="المنصور",
        street="شارع 14 رمضان",
        landmark="قرب الساعة",
        tier=tier,
        rep=rep1,
        debt_limit=10_000_000,
        active=True,
        status=CustomerStatus.ACTIVE,
        approved_by=mgr,
    )
    cust2 = Customer.objects.create(
        name="سوق الكرادة",
        owner_name="أبو محمد",
        phone="07750000009",
        city="بغداد",
        area="الكرادة",
        street="شارع 62",
        landmark="ساحة الواثق",
        tier=tier,
        rep=rep1,
        debt_limit=5_000_000,
        active=True,
        status=CustomerStatus.ACTIVE,
        approved_by=mgr,
    )
    ShopMembership.objects.create(user=shop_user, customer=cust1, is_primary=True, is_active_shop=True)
    ShopMembership.objects.create(user=shop_user, customer=cust2, is_primary=False, is_active_shop=False)
    return {
        "tier": tier,
        "wh1": wh1,
        "wh2": wh2,
        "mgr": mgr,
        "acct": acct,
        "keeper1": keeper1,
        "keeper2": keeper2,
        "staff1": staff1,
        "rep1": rep1,
        "rep2": rep2,
        "shop_user": shop_user,
        "cust1": cust1,
        "cust2": cust2,
    }


@pytest.mark.django_db
def test_site_branding_custom_fields_and_automation_rules(erp_seed):
    mgr_cli, _, _ = _auth_client("07750000001")
    rep_cli, _, _ = _auth_client("07750000006")
    acct_cli, _, _ = _auth_client("07750000002")

    # 1. Anonymous GET /site hides advanced security config while serving public branding
    anon = APIClient()
    r_pub = anon.get("/api/v1/site")
    assert r_pub.status_code == 200
    assert r_pub.json()["data"]["brand"]["name"]
    assert r_pub.json()["data"]["profiles"] == []

    # 2. Configure site branding (real PNG via storage backend), custom fields, and automation rules
    site_payload = {
        "brand": {"name": "النهرين التجريبي", "name_en": "Al-Nahrain Pro", "logo": _png_data_url()},
        "categories": [{"ar": "زيوت", "en": "Oils"}, {"ar": "حبوب", "en": "Grains"}],
        "fields": [
            {
                "id": "tax_no",
                "object": "customer",
                "label": "الرقم الضريبي",
                "type": "text",
                "required": False,
                "view_roles": ["manager", "accountant"],
                "edit_roles": ["manager", "accountant"],
                "active": True,
            },
            {
                "id": "po_ref",
                "object": "order",
                "label": "مرجع الشراء",
                "type": "text",
                "required": True,
                "view_roles": [],
                "edit_roles": [],
                "active": True,
            },
        ],
        "rules": [
            {
                "id": "big_order",
                "name": "طلب كبير",
                "active": True,
                "trigger": "order_created",
                "conditions": [{"field": "total", "op": "gte", "value": 100000}],
                "approval": True,
                "notify": ["manager", "accountant"],
                "message": "طلب {number} بمبلغ {amount} للزبون {customer}",
            }
        ],
    }
    assert rep_cli.patch("/api/v1/settings/site", site_payload, format="json").status_code == 403
    r_save = mgr_cli.patch("/api/v1/settings/site", site_payload, format="json")
    assert r_save.status_code == 200, r_save.content
    logo_url = r_save.json()["data"]["brand"]["logo"]
    assert logo_url and not logo_url.startswith("data:image")

    # Authenticated GET /site returns advanced fields & rules
    r_site_auth = mgr_cli.get("/api/v1/site")
    assert len(r_site_auth.json()["data"]["rules"]) == 1

    # 3. Custom fields RBAC & required validation
    cust1 = erp_seed["cust1"]
    assert rep_cli.patch(f"/api/v1/custom/customer/{cust1.id}", {"tax_no": "TX-99"}, format="json").status_code == 403
    r_cf = acct_cli.patch(f"/api/v1/custom/customer/{cust1.id}", {"tax_no": "TX-99"}, format="json")
    assert r_cf.status_code == 200
    assert r_cf.json()["data"]["tax_no"] == "TX-99"

    # Accountant sees tax_no on customer; rep does not (`visible: ["manager", "accountant"]`)
    c_acct = acct_cli.get(f"/api/v1/customers/{cust1.id}").json()["data"]
    c_rep = rep_cli.get(f"/api/v1/customers/{cust1.id}").json()["data"]
    assert c_acct["custom"]["tax_no"] == "TX-99"
    assert "tax_no" not in (c_rep.get("custom") or {})

    # 4. Seed item + batch, then test order creation required custom field + automation rule approval
    it = mgr_cli.post(
        "/api/v1/items",
        {
            "name": "زيت طبخ 1 لتر",
            "name_en": "Cooking Oil 1L",
            "barcode": "OIL-T-1",
            "category": "زيوت",
            "pieces_per_carton": 10,
            "min_stock": 20,
            "weight_kg": 1.0,
            "prices": [{"tier_id": erp_seed["tier"].id, "carton_price": 60000, "piece_price": 6250}],
        },
        format="json",
    ).json()["data"]
    exp = (baghdad_today() + timedelta(days=200)).isoformat()
    r_rec = mgr_cli.post(
        "/api/v1/receipts",
        {
            "warehouse_id": erp_seed["wh1"].id,
            "supplier": "مورد",
            "lines": [
                {"item_id": it["id"], "unit": "carton", "qty": 20, "purchase_price": 5000, "expiry_date": exp},
            ],
        },
        format="json",
    )
    assert r_rec.status_code == 201, r_rec.content

    # Missing required order custom field `po_ref` -> 422 VALIDATION
    r_missing = rep_cli.post(
        "/api/v1/orders",
        {
            "customer_id": cust1.id,
            "warehouse_id": erp_seed["wh1"].id,
            "lines": [{"item_id": it["id"], "unit": "carton", "qty": 2}],
        },
        format="json",
    )
    assert r_missing.status_code in (400, 422)

    # Valid order triggers automation rule `big_order` (total 120,000 >= 100,000)
    r_ord = rep_cli.post(
        "/api/v1/orders",
        {
            "customer_id": cust1.id,
            "warehouse_id": erp_seed["wh1"].id,
            "lines": [{"item_id": it["id"], "unit": "carton", "qty": 2}],
            "custom": {"po_ref": "PO-555"},
        },
        format="json",
    )
    assert r_ord.status_code == 201, r_ord.content
    ord_data = r_ord.json()["data"]
    assert ord_data["status"] == "needs_approval"
    assert "rule" in ord_data["approval_for"]
    assert "طلب كبير" in ord_data["rule_note"]
    assert ord_data["custom"]["po_ref"] == "PO-555"

    # RuleRun log recorded and accountant cannot approve a "rule" order (only manager)
    r_runs = mgr_cli.get("/api/v1/setup/rule-runs")
    assert r_runs.status_code == 200 and len(r_runs.json()["data"]) >= 1
    assert acct_cli.post(f"/api/v1/orders/{ord_data['id']}/approve").status_code == 403
    assert mgr_cli.post(f"/api/v1/orders/{ord_data['id']}/approve").status_code == 200


@pytest.mark.django_db
def test_permission_profiles_hours_redactions_and_health(erp_seed):
    mgr_cli, _, _ = _auth_client("07750000001")
    acct_id = erp_seed["acct"].id
    now_local = timezone.localtime(timezone.now())
    # Denied hour window guaranteed NOT to contain now (e.g. 2 hours window starting +2h from now)
    start_h = (now_local.hour + 2) % 24
    end_h = (start_h + 1) % 24
    outside_hours = {
        "days": [0, 1, 2, 3, 4, 5, 6],
        "from": f"{start_h:02d}:00",
        "to": f"{end_h:02d}:00",
    }

    r_site = mgr_cli.patch(
        "/api/v1/settings/site",
        {
            "profiles": [
                {
                    "id": "acct_restricted",
                    "name": "محاسب مقيد",
                    "role": "accountant",
                    "denied": ["/expenses"],
                    "limits": {"no_profit": True, "no_debt": True},
                    "read_only": True,
                    "deny_actions": ["payments", "returns"],
                },
                {
                    "id": "night_lock",
                    "name": "ساعات مغلقة",
                    "role": "accountant",
                    "denied": [],
                    "limits": {},
                    "hours": outside_hours,
                },
            ]
        },
        format="json",
    )
    assert r_site.status_code == 200, r_site.content

    # Assign restricted profile to accountant
    assert mgr_cli.post(f"/api/v1/users/{acct_id}/profile", {"profile_id": "acct_restricted"}, format="json").status_code == 200
    acct_cli, _, _ = _auth_client("07750000002")

    # Denied page (/expenses -> /api/v1/expenses) -> 403
    assert acct_cli.get("/api/v1/expenses").status_code == 403
    # Read-only blocks POST/PATCH on domain endpoints -> 403, while allowing self photo/password
    assert acct_cli.post("/api/v1/orders", {}, format="json").status_code == 403
    r_photo = acct_cli.post("/api/v1/me/photo", {"photo": _png_data_url()}, format="json")
    assert r_photo.status_code == 200
    # no_profit + no_debt redact dashboard profit/expenses/net and debt KPIs
    dash = acct_cli.get("/api/v1/dashboard").json()["data"]
    assert "month_profit" not in dash and "month_net" not in dash
    assert dash["total_debt"] == 0 and dash["today_collected"] == 0

    # Assign outside-hours profile -> API fails 401 and fresh login fails 403
    mgr_cli.post(f"/api/v1/users/{acct_id}/profile", {"profile_id": "night_lock"}, format="json")
    assert acct_cli.get("/api/v1/dashboard").status_code == 401
    r_bad_time = APIClient().post("/api/v1/auth/login", {"phone": "07750000002", "password": "StrongPass!2026"}, format="json")
    assert r_bad_time.status_code == 403
    mgr_cli.post(f"/api/v1/users/{acct_id}/profile", {"profile_id": None}, format="json")

    # Health & weak password check + user soft delete + phone reuse
    weak_res = mgr_cli.post(
        "/api/v1/users",
        {"phone": "07750000022", "password": "123456", "name": "ضعيف", "role": Role.STAFF, "warehouse_id": erp_seed["wh1"].id},
        format="json",
    )
    assert weak_res.status_code == 201
    weak_user = User.objects.get(id=weak_res.json()["data"]["id"])
    r_health = mgr_cli.get("/api/v1/setup/health")
    assert r_health.status_code == 200
    hdata = r_health.json()["data"]
    assert "score" in hdata and "checks" in hdata
    assert any(w["id"] == weak_user.id for w in hdata["users"]["weak"])

    # Change own password enforces current password & minimum length, updates weak_password, and bumps token_version
    weak_cli, _, _ = _auth_client("07750000022", "123456")
    assert weak_cli.post("/api/v1/me/password", {"current": "wrong", "next": "ValidNew!99"}, format="json").status_code == 422
    assert weak_cli.post("/api/v1/me/password", {"current": "123456", "next": "12345"}, format="json").status_code == 422
    assert weak_cli.post("/api/v1/me/password", {"current": "123456", "next": "ValidNew!99"}, format="json").status_code == 200
    weak_user.refresh_from_db()
    assert weak_user.weak_password is False

    # Soft delete user frees phone for reuse; manager cannot self-delete
    assert mgr_cli.delete(f"/api/v1/users/{erp_seed['mgr'].id}").status_code == 409
    assert mgr_cli.delete(f"/api/v1/users/{weak_user.id}").status_code == 200
    r_reuse = mgr_cli.post(
        "/api/v1/users",
        {"phone": "07750000022", "password": "ValidNew!99", "name": "بديل", "role": Role.STAFF, "warehouse_id": erp_seed["wh1"].id},
        format="json",
    )
    assert r_reuse.status_code == 201


@pytest.mark.django_db
def test_warehouses_layout_cover_delete_and_catalog_quick_category_rename(erp_seed):
    mgr_cli, _, _ = _auth_client("07750000001")
    k1_cli, _, _ = _auth_client("07750000003")

    # Create a clean unused warehouse -> delete succeeds; deleting wh1 with active keeper -> 409
    wh_tmp = mgr_cli.post("/api/v1/warehouses", {"name": "مخزن مؤقت", "name_en": "Temp WH", "capacity": 500}, format="json").json()["data"]
    assert mgr_cli.delete(f"/api/v1/warehouses/{erp_seed['wh1'].id}").status_code == 409
    assert mgr_cli.delete(f"/api/v1/warehouses/{wh_tmp['id']}").status_code == 200

    # Layout save (keeper1 allowed for wh1, denied for wh2 until temporary cover assigned)
    layout = {"cols": 4, "rows": 3, "zones": [{"code": "A", "name": "رفوف أ", "x": 0, "y": 0, "w": 2, "h": 2}], "places": {}}
    assert k1_cli.patch(f"/api/v1/warehouses/{erp_seed['wh1'].id}/layout", layout, format="json").status_code == 200
    assert k1_cli.patch(f"/api/v1/warehouses/{erp_seed['wh2'].id}/layout", layout, format="json").status_code == 403

    # Cover wh2 with keeper1 -> keeper1 now works in wh2
    until_iso = (datetime.now(tz=UTC) + timedelta(hours=8)).isoformat()
    r_cov = mgr_cli.post(
        f"/api/v1/warehouses/{erp_seed['wh2'].id}/cover",
        {"user_id": erp_seed["keeper1"].id, "hours": 8, "until": until_iso},
        format="json",
    )
    assert r_cov.status_code == 200
    assert k1_cli.patch(f"/api/v1/warehouses/{erp_seed['wh2'].id}/layout", layout, format="json").status_code == 200

    # Item quick edit + category rename
    it = mgr_cli.post(
        "/api/v1/items",
        {
            "name": "أرز هندي 5 كغم",
            "name_en": "Basmati Rice 5kg",
            "barcode": "RICE-Q1",
            "category": "حبوب",
            "pieces_per_carton": 4,
            "min_stock": 10,
            "prices": [{"tier_id": erp_seed["tier"].id, "carton_price": 40000, "piece_price": 10500}],
        },
        format="json",
    ).json()["data"]
    assert "weight_kg" not in it  # omitted weight_kg uses guess_weight_kg fallback

    r_q = mgr_cli.patch(
        f"/api/v1/items/{it['id']}/quick",
        {"name_en": "Basmati Rice 5kg Premium", "tier_id": erp_seed["tier"].id, "carton_price": 42000},
        format="json",
    )
    assert r_q.status_code == 200 and r_q.json()["data"]["name_en"] == "Basmati Rice 5kg Premium"
    assert r_q.json()["data"]["prices"][0]["carton_price"] == 42000

    r_up = mgr_cli.patch(
        f"/api/v1/items/{it['id']}",
        {"weight_kg": 5.0, "min_stock": 25},
        format="json",
    )
    assert r_up.status_code == 200 and r_up.json()["data"]["weight_kg"] == 5.0 and r_up.json()["data"]["min_stock"] == 25

    r_ren = mgr_cli.post("/api/v1/categories/rename", {"from": "حبوب", "to": "رز وبقوليات"}, format="json")
    assert r_ren.status_code == 200
    assert mgr_cli.get("/api/v1/items").json()["data"][0]["category"] == "رز وبقوليات"


@pytest.mark.django_db
def test_vehicles_shipment_splitting_loading_and_parent_sync(erp_seed):
    mgr_cli, _, _ = _auth_client("07750000001")
    k1_cli, _, _ = _auth_client("07750000003")
    rep1_cli, _, _ = _auth_client("07750000006")
    rep2_cli, _, _ = _auth_client("07750000007")
    shop_cli, _, _ = _auth_client("07750000008")

    # Vehicles: small van (50kg, driver=rep1), big truck (200kg, driver=rep2)
    v1 = mgr_cli.post("/api/v1/vehicles", {"name": "فان صغيرة", "capacity_kg": 50, "driver_id": erp_seed["rep1"].id, "active": True}, format="json").json()["data"]
    v2 = mgr_cli.post("/api/v1/vehicles", {"name": "شاحنة كبيرة", "capacity_kg": 200, "driver_id": erp_seed["rep2"].id, "active": True}, format="json").json()["data"]

    it = mgr_cli.post(
        "/api/v1/items",
        {
            "name": "سكر 10 كغم",
            "name_en": "Sugar 10kg",
            "barcode": "SUG-10",
            "category": "حبوب",
            "pieces_per_carton": 1,
            "min_stock": 5,
            "weight_kg": 10.0,
            "prices": [{"tier_id": erp_seed["tier"].id, "carton_price": 15000, "piece_price": 15000}],
        },
        format="json",
    ).json()["data"]
    exp = (baghdad_today() + timedelta(days=150)).isoformat()
    r_rec2 = mgr_cli.post(
        "/api/v1/receipts",
        {
            "warehouse_id": erp_seed["wh1"].id,
            "supplier": "مورد",
            "lines": [{"item_id": it["id"], "unit": "carton", "qty": 20, "purchase_price": 11000, "expiry_date": exp}],
        },
        format="json",
    )
    assert r_rec2.status_code == 201, r_rec2.content

    # Order 8 cartons = 80kg (too heavy for 50kg van v1!)
    ord_data = mgr_cli.post(
        "/api/v1/orders",
        {
            "customer_id": erp_seed["cust1"].id,
            "warehouse_id": erp_seed["wh1"].id,
            "lines": [{"item_id": it["id"], "unit": "carton", "qty": 8}],
        },
        format="json",
    ).json()["data"]
    prep = k1_cli.post(f"/api/v1/orders/{ord_data['id']}/prepare", {"warehouses": {}}, format="json").json()["data"]
    assert prep["delivery"]["vehicle"]["id"] == v1["id"]

    # Trying to load 80kg onto 50kg v1 fails 409 BAD_STATE with suggestion for v2 ("شاحنة كبيرة")
    r_over = k1_cli.post(f"/api/v1/orders/{ord_data['id']}/delivery/load")
    assert r_over.status_code == 409 and v2["name"] in r_over.json()["message"]

    # Split 8 cartons -> 4 cartons on v1 (40kg) + 4 cartons on v2 (40kg)
    r_split = k1_cli.post(
        f"/api/v1/orders/{ord_data['id']}/shipments",
        {
            "loads": [
                {"vehicle_id": v1["id"], "lines": [{"item_id": it["id"], "unit": "carton", "qty": 4}]},
                {"vehicle_id": v2["id"], "lines": [{"item_id": it["id"], "unit": "carton", "qty": 4}]},
            ]
        },
        format="json",
    )
    assert r_split.status_code == 200, r_split.content
    split_parent = r_split.json()["data"]
    assert split_parent["delivery"] is None
    assert len(split_parent["shipments"]) == 2

    s1_id, s2_id = split_parent["shipments"]
    # Load & deliver both child shipments -> parent order transitions to `confirmed`
    assert k1_cli.post(f"/api/v1/orders/{s1_id}/delivery/load").status_code == 200
    assert k1_cli.post(f"/api/v1/orders/{s2_id}/delivery/load").status_code == 200

    s1_shop = shop_cli.get(f"/api/v1/orders/{s1_id}").json()["data"]
    s2_shop = shop_cli.get(f"/api/v1/orders/{s2_id}").json()["data"]

    rep1_cli.post("/api/v1/deliveries/dispatch", {"code": f"NAHRAIN:DLV:{s1_shop['delivery']['code']}"}, format="json")
    rep2_cli.post("/api/v1/deliveries/dispatch", {"code": f"NAHRAIN:DLV:{s2_shop['delivery']['code']}"}, format="json")

    photo = _png_data_url()
    r_d1 = rep1_cli.post(f"/api/v1/orders/{s1_id}/deliver", {"pin": s1_shop["delivery"]["pin"], "photo": photo}, format="json")
    assert r_d1.status_code == 200
    # After 1 of 2 delivered, parent is still approved
    assert mgr_cli.get(f"/api/v1/orders/{ord_data['id']}").json()["data"]["status"] == "approved"

    r_d2 = rep2_cli.post(f"/api/v1/orders/{s2_id}/deliver", {"pin": s2_shop["delivery"]["pin"], "photo": photo}, format="json")
    assert r_d2.status_code == 200
    # After 2 of 2 delivered, parent is confirmed!
    assert mgr_cli.get(f"/api/v1/orders/{ord_data['id']}").json()["data"]["status"] == "confirmed"


@pytest.mark.django_db
def test_expenses_dashboard_net_rep_targets_and_shop_self_delete(erp_seed):
    mgr_cli, _, _ = _auth_client("07750000001")
    acct_cli, _, _ = _auth_client("07750000002")
    rep1_cli, _, _ = _auth_client("07750000006")
    shop_cli, _, _ = _auth_client("07750000008")
    today_iso = baghdad_today().isoformat()
    month_iso = today_iso[:7]

    # Set expense_approval_above = 100,000 -> small auto-approves, large awaits manager
    mgr_cli.patch("/api/v1/settings", {"expense_approval_above": 100000}, format="json")

    r_small = acct_cli.post(
        "/api/v1/expenses",
        {
            "category": "وقود",
            "amount": 50000,
            "method": "cash",
            "paid_to": "محطة الكرخ",
            "date": today_iso,
            "warehouse_id": erp_seed["wh1"].id,
            "photo": _png_data_url(),
        },
        format="json",
    )
    assert r_small.status_code == 201 and r_small.json()["data"]["status"] == "approved"
    assert "/api/v1/media/signed?token=" in r_small.json()["data"]["photo"]

    r_big = acct_cli.post(
        "/api/v1/expenses",
        {
            "category": "رواتب",
            "amount": 250000,
            "method": "transfer",
            "paid_to": "موظف",
            "date": today_iso,
            "warehouse_id": erp_seed["wh1"].id,
        },
        format="json",
    )
    big_id = r_big.json()["data"]["id"]
    assert r_big.json()["data"]["status"] == "pending"

    # Non-manager cannot approve; manager approves -> month_expenses = 50k + 250k = 300k
    assert acct_cli.post(f"/api/v1/expenses/{big_id}/decide", {"ok": True}, format="json").status_code == 403
    assert mgr_cli.post(f"/api/v1/expenses/{big_id}/decide", {"ok": True}, format="json").json()["data"]["status"] == "approved"
    dash = mgr_cli.get("/api/v1/dashboard").json()["data"]
    assert dash["month_expenses"] == 300000
    assert dash["month_net"] == dash["month_profit"] - 300000

    # Cancel small expense -> excluded from month_expenses
    assert mgr_cli.post(f"/api/v1/expenses/{r_small.json()['data']['id']}/cancel", {"reason": "خطأ"}, format="json").status_code == 200
    assert mgr_cli.get("/api/v1/dashboard").json()["data"]["month_expenses"] == 250000

    # Rep targets
    targets_mgr = mgr_cli.get(f"/api/v1/reps/targets?month={month_iso}").json()["data"]
    targets_rep = rep1_cli.get(f"/api/v1/reps/targets?month={month_iso}").json()["data"]
    assert len(targets_mgr) >= 1 and targets_mgr[0]["rank"] == 1
    assert len(targets_rep) == 1 and targets_rep[0]["rep"]["id"] == erp_seed["rep1"].id

    # Shop removes 1 of 2 shops, then deletes entire account with password verification
    assert shop_cli.post(f"/api/v1/me/shops/{erp_seed['cust2'].id}/remove", {"password": "wrong"}, format="json").status_code == 422
    r_rm = shop_cli.post(f"/api/v1/me/shops/{erp_seed['cust2'].id}/remove", {"password": "StrongPass!2026"}, format="json")
    assert r_rm.status_code == 200
    assert erp_seed["cust2"].id not in [s["id"] for s in (r_rm.json()["data"].get("shops") or [])]

    assert shop_cli.post("/api/v1/me/delete", {"password": "StrongPass!2026"}, format="json").status_code == 200
    # Re-auth after delete fails
    assert shop_cli.get("/api/v1/me").status_code == 401


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_websocket_jwt_token_query_auth_matches_frontend(erp_seed):
    from channels.db import database_sync_to_async

    mgr_cli, mgr_token, _ = await database_sync_to_async(_auth_client)("07750000001")
    comm = WebsocketCommunicator(application, f"/ws?token={mgr_token}")
    connected, _ = await comm.connect()
    assert connected is True
    await comm.send_json_to({"action": "subscribe", "topics": ["notifications", "orders", "location"]})
    await comm.send_json_to({"action": "ping"})
    pong = await comm.receive_json_from()
    assert pong["type"] == "pong"
    await comm.disconnect()

    # Invalid token rejected with 4001
    bad_comm = WebsocketCommunicator(application, "/ws?token=invalid.jwt.value")
    bad_ok, close_code = await bad_comm.connect()
    assert bad_ok is False and close_code == 4001


@pytest.mark.django_db
def test_delivery_rep_profile_denied_items_allows_app_shell_reads(erp_seed):
    mgr_cli, _, _ = _auth_client("07750000001")
    rep2 = erp_seed["rep2"]
    rep2.rep_kinds = ["delivery"]
    rep2.save(update_fields=["rep_kinds"])

    r_site = mgr_cli.patch(
        "/api/v1/settings/site",
        {
            "profiles": [
                {
                    "id": "prof_dlv_rep",
                    "name": "سائق توصيل",
                    "role": "rep",
                    "denied": ["/items", "/stocktakes", "/supply", "/reports"],
                    "limits": {},
                }
            ]
        },
        format="json",
    )
    assert r_site.status_code == 200, r_site.content
    assert mgr_cli.post(f"/api/v1/users/{rep2.id}/profile", {"profile_id": "prof_dlv_rep"}, format="json").status_code == 200

    rep_cli, _, _ = _auth_client(rep2.phone)
    # AppShell reference reads required on /deliveries/ must succeed even when /items is in denied
    assert rep_cli.get("/api/v1/items").status_code == 200
    assert rep_cli.get("/api/v1/orders").status_code == 200
    assert rep_cli.get("/api/v1/lookups").status_code == 200
    # Write operations on /items and unrelated denied endpoints remain blocked
    assert rep_cli.post("/api/v1/items", {"name": "forbidden"}, format="json").status_code == 403
    assert rep_cli.get("/api/v1/reports/sales").status_code == 403


@pytest.mark.django_db
def test_site_serializer_normalizes_legacy_profiles_apps_fields_and_rules(erp_seed):
    from apps.accounts.models import SiteConfig

    cfg = SiteConfig.get_solo()
    cfg.profiles = [
        {"id": "p_legacy", "name": "ملف قديم", "role": "staff", "denied": ["/reports"], "can": {"orders": True}}
    ]
    cfg.apps = [{"id": "a_legacy", "name": "تطبيق قديم", "color": "#1d4ed8", "pages": ["/orders"]}]
    cfg.fields = [{"id": "f_legacy", "object": "customer", "label": "حقل قديم", "type": "list"}]
    cfg.rules = [{"id": "r_legacy", "name": "قاعدة قديمة", "trigger": "order_created"}]
    cfg.save()

    mgr_cli, _, _ = _auth_client("07750000001")
    r_site = mgr_cli.get("/api/v1/site")
    assert r_site.status_code == 200
    data = r_site.json()["data"]

    prof = data["profiles"][0]
    assert prof["limits"] == {}
    assert prof["denied"] == ["/reports"]
    assert "can" not in prof

    app = data["apps"][0]
    assert app["roles"] == []
    assert app["profiles"] == []
    assert app["icon"] == "grid"
    assert app["active"] is True

    fld = data["fields"][0]
    assert fld["view_roles"] == []
    assert fld["edit_roles"] == []
    assert fld["active"] is True
    assert fld["required"] is False
    assert fld["options"] == []

    rule = data["rules"][0]
    assert rule["active"] is True
    assert rule["conditions"] == []
    assert rule["notify"] == []
    assert rule["approval"] is False


