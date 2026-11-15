"""Comprehensive verification of «قواعد العمل / الإعدادات المتقدمة» (settings.advanced)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from rest_framework.test import APIClient

from apps.accounts.models import OtpChallenge, PriceTier, Role, SettingsChange, SystemSetting, User
from apps.catalog.models import Item, ItemPrice
from apps.common.utils import baghdad_now, baghdad_today
from apps.customers.models import Customer, CustomerStatus, ShopMembership
from apps.deliveries.models import Delivery, DeliveryStatus
from apps.inventory.models import Batch
from apps.notifications.models import Notification
from apps.orders.models import Order, OrderStatus
from apps.payments.models import Payment
from apps.warehouses.models import Warehouse


@pytest.fixture
def rules_setup(db):
    wh = Warehouse.objects.create(name="مخزن ١", name_en="WH1", code="W1")
    t1 = PriceTier.objects.create(name="جملة", name_en="Wholesale", sort_order=1)
    t2 = PriceTier.objects.create(name="تجزئة", name_en="Retail", sort_order=2)
    SystemSetting.get_solo()

    mgr = User.objects.create_user(phone="07709000001", password="Password123", name="Manager", role=Role.MANAGER)
    acct = User.objects.create_user(phone="07709000002", password="Password123", name="Accountant", role=Role.ACCOUNTANT)
    keeper = User.objects.create_user(
        phone="07709000003", password="Password123", name="Keeper", role=Role.WAREHOUSE, warehouse=wh
    )
    staff = User.objects.create_user(
        phone="07709000008", password="Password123", name="Staff", role=Role.STAFF, warehouse=wh
    )
    rep = User.objects.create_user(
        phone="07709000004",
        password="Password123",
        name="Rep One",
        role=Role.REP,
        rep_kinds=["sales", "delivery"],
        rep_categories=["مواد غذائية"],
    )

    cust = Customer.objects.create(
        name="محل السلام",
        owner_name="سلام",
        phone="07709000005",
        city="كربلاء",
        area="حي الحسين",
        street="شارع 1",
        landmark="قرب المسجد",
        lat=32.600000,
        lng=44.000000,
        tier=t1,
        rep=rep,
        debt_limit=50_000,
        status=CustomerStatus.ACTIVE,
        active=True,
    )
    shop_u = User.objects.create_user(phone="07709000005", password="Password123", name="سلام", role=Role.SHOP)
    ShopMembership.objects.create(user=shop_u, customer=cust, is_primary=True, is_active_shop=True)

    item = Item.objects.create(
        name="رز بسمتي",
        name_en="Basmati Rice",
        category="مواد غذائية",
        pieces_per_carton=10,
        active=True,
    )
    ItemPrice.objects.create(item=item, tier=t1, carton_price=10_120, piece_price=1_020)
    ItemPrice.objects.create(item=item, tier=t2, carton_price=11_000, piece_price=1_100)
    Batch.objects.create(
        item=item,
        warehouse=wh,
        qty_received=500,
        qty_left=500,
        purchase_price=800,
        expiry_date=baghdad_today() + timedelta(days=180),
    )

    return {
        "wh": wh,
        "t1": t1,
        "t2": t2,
        "mgr": mgr,
        "acct": acct,
        "keeper": keeper,
        "staff": staff,
        "rep": rep,
        "cust": cust,
        "shop_u": shop_u,
        "item": item,
    }


def _login(phone: str, password: str = "Password123") -> APIClient:
    c = APIClient()
    res = c.post("/api/v1/auth/login", {"phone": phone, "password": password}, format="json")
    assert res.status_code == 200, res.json()
    c.credentials(HTTP_AUTHORIZATION=f"Bearer {res.json()['data']['token']}")
    return c


@pytest.mark.django_db
def test_settings_validation_minimal_storage_history_and_public_site_live(rules_setup):
    mgr = _login("07709000001")
    acct = _login("07709000002")

    # 1. Invalid rules return 422 with exact field keys from checkRules()
    bad = mgr.patch(
        "/api/v1/settings",
        {
            "advanced": {
                "staff_discount_max": 20,
                "discount_max": 10,
                "round_to": 333,
                "reverse_roles": ["accountant"],
                "signup_tier_id": 999999,
                "otp": {"code_minutes": 99},
                "announcement": {"text": " ", "color": "amber", "until": "not-a-date"},
            }
        },
        format="json",
    )
    assert bad.status_code == 422, bad.json()
    assert bad.json()["code"] == "VALIDATION"
    errs = bad.json()["errors"]
    assert "discount_max" in errs
    assert "round_to" in errs
    assert "reverse_roles" in errs
    assert "signup_tier_id" in errs
    assert "otp.code_minutes" in errs
    assert "announcement.text" in errs
    assert "announcement.until" in errs

    # 2. Saving a partial change stores ONLY non-default rules and logs history
    ok = mgr.patch(
        "/api/v1/settings",
        {
            "advanced": {
                "round_to": 500,
                "min_shop_order": 25_000,
                "features": {"online_pay": False},
                "maintenance": {"on": True, "message": "صيانة قصيرة", "message_en": "Short maintenance"},
                "announcement": {"text": "خصم اليوم", "color": "green"},
            }
        },
        format="json",
    )
    assert ok.status_code == 200, ok.json()
    adv = ok.json()["data"]["advanced"]
    assert adv["round_to"] == 500
    assert "pin_tries" not in adv  # defaults stripped cleanly!
    assert adv["features"] == {"online_pay": False}

    # Public unauthenticated GET /site exposes `live` (including `names` and `users_page`)
    anon = APIClient().get("/api/v1/site")
    assert anon.status_code == 200
    live = anon.json()["data"]["live"]
    assert live["features"]["online_pay"] is False
    assert live["maintenance"]["on"] is True
    assert live["maintenance"]["message"] == "صيانة قصيرة"
    assert live["announcement"]["text"] == "خصم اليوم"
    assert live["password_min"] == 6
    assert live["names"]["currency_ar"] == "د.ع"
    assert live["names"]["currency_en"] == "IQD"
    assert live["names"]["invoice_footer"] == ""
    assert live["users_page"] == {"roles": ["manager"], "allow": [], "deny": []}

    # Creating a permission profile via PATCH /settings/site succeeds and preserves `live.names`
    site_cur = mgr.get("/api/v1/site").json()["data"]
    site_cur["profiles"] = [
        {"id": "p_test_rule", "name": "مساعد محاسب", "role": "accountant", "denied": [], "limits": {}}
    ]
    saved_site = mgr.patch("/api/v1/settings/site", site_cur, format="json")
    assert saved_site.status_code == 200, saved_site.json()
    assert len(saved_site.json()["data"]["profiles"]) == 1
    assert saved_site.json()["data"]["live"]["names"]["currency_ar"] == "د.ع"

    # Accountant cannot view settings history
    assert acct.get("/api/v1/settings/history").status_code == 403
    hist = mgr.get("/api/v1/settings/history")
    assert hist.status_code == 200
    entry = hist.json()["data"][0]
    keys = {ch["key"] for ch in entry["changes"]}
    assert "advanced.round_to" in keys
    assert "advanced.min_shop_order" in keys
    assert "advanced.features.online_pay" in keys
    assert "advanced.announcement" in keys

    # Reset via how="reset" clears advanced rules cleanly and logs how="reset"
    rst = mgr.patch("/api/v1/settings", {"advanced": {}, "how": "reset"}, format="json")
    assert rst.status_code == 200
    assert "advanced" not in rst.json()["data"]
    assert SettingsChange.objects.order_by("-id").first().how == "reset"


@pytest.mark.django_db
def test_order_rules_maintenance_min_order_round_over_limit_edit_window(rules_setup):
    mgr = _login("07709000001")
    rep = _login("07709000004")
    shop = _login("07709000005")
    item = rules_setup["item"]
    wh = rules_setup["wh"]
    cust = rules_setup["cust"]

    # 1. Maintenance blocks shop orders with 503 MAINTENANCE (rep still works)
    mgr.patch(
        "/api/v1/settings",
        {"advanced": {"maintenance": {"on": True, "message": "صيانة للمتاجر", "message_en": "Shop maintenance"}}},
        format="json",
    )
    m_res = shop.post("/api/v1/orders", {"warehouse_id": wh.id, "lines": [{"item_id": item.id, "unit": "carton", "qty": 1}]}, format="json")
    assert m_res.status_code == 503
    assert m_res.json()["code"] == "MAINTENANCE"

    # 2. Turn maintenance off, enforce min_shop_order=25000, round_to=500, over_limit="block", shop_edit_minutes=5
    mgr.patch(
        "/api/v1/settings",
        {
            "advanced": {
                "maintenance": {"on": False},
                "min_shop_order": 25_000,
                "round_to": 500,
                "over_limit": "block",
                "shop_edit_minutes": 5,
                "staff_discount_max": 2,
                "discount_max": 10,
                "auto_confirm_rep_orders": True,
            }
        },
        format="json",
    )

    # Below min_shop_order -> 422 MIN_ORDER with errors.lines
    small = shop.post("/api/v1/orders", {"warehouse_id": wh.id, "lines": [{"item_id": item.id, "unit": "carton", "qty": 1}]}, format="json")
    assert small.status_code == 422
    assert small.json()["code"] == "MIN_ORDER"
    assert "lines" in small.json()["errors"]

    # 3 cartons = 30,360 -> rounded to nearest 500 = 30,500 (<= 50,000 debt limit)
    ok_ord = shop.post("/api/v1/orders", {"warehouse_id": wh.id, "lines": [{"item_id": item.id, "unit": "carton", "qty": 3}]}, format="json")
    assert ok_ord.status_code == 201, ok_ord.json()
    ord_data = ok_ord.json()["data"]
    assert ord_data["total"] == 30_500

    # 6 cartons > 50,000 debt_limit -> over_limit="block" raises 409 DEBT_LIMIT!
    blk = shop.post("/api/v1/orders", {"warehouse_id": wh.id, "lines": [{"item_id": item.id, "unit": "carton", "qty": 6}]}, format="json")
    assert blk.status_code == 409
    assert blk.json()["code"] == "DEBT_LIMIT"

    # Simulate order created 10 minutes ago -> shop PATCH returns 409 EDIT_CLOSED
    Order.objects.filter(id=ord_data["id"]).update(created_at=datetime.now(tz=UTC) - timedelta(minutes=10))
    closed_edit = shop.patch(f"/api/v1/orders/{ord_data['id']}", {"note": "late"}, format="json")
    assert closed_edit.status_code == 409
    assert closed_edit.json()["code"] == "EDIT_CLOSED"

    # over_limit="allow" passes with manager notification + auto_confirm_rep_orders sets accepted_by auto
    mgr.patch(
        "/api/v1/settings",
        {"advanced": {"over_limit": "allow", "auto_confirm_rep_orders": True, "staff_discount_max": 2, "discount_max": 10}},
        format="json",
    )
    rep_ord = rep.post(
        "/api/v1/orders",
        {"customer_id": cust.id, "warehouse_id": wh.id, "lines": [{"item_id": item.id, "unit": "carton", "qty": 6}]},
        format="json",
    )
    assert rep_ord.status_code == 201, rep_ord.json()
    assert rep_ord.json()["data"]["status"] == "approved"
    assert rep_ord.json()["data"]["accepted_by"]["name"] == "اعتماد تلقائي"
    assert Notification.objects.filter(target_role=Role.MANAGER, kind="needs_approval", ref_id=str(rep_ord.json()["data"]["id"])).exists()

    # Dynamic discount limits: >10% rejected for rep, 3% (>2% staff_discount_max) needs manager approval
    disc_ord = rep.post(
        "/api/v1/orders",
        {"customer_id": cust.id, "warehouse_id": wh.id, "lines": [{"item_id": item.id, "unit": "carton", "qty": 1}]},
        format="json",
    ).json()["data"]
    bad_disc = rep.post(f"/api/v1/orders/{disc_ord['id']}/discount", {"percent": 15}, format="json")
    assert bad_disc.status_code == 400
    assert bad_disc.json()["code"] == "VALIDATION"
    ok_disc = rep.post(f"/api/v1/orders/{disc_ord['id']}/discount", {"percent": 3}, format="json")
    assert ok_disc.status_code == 200
    assert "discount" in ok_disc.json()["data"]["approval_for"]


@pytest.mark.django_db
def test_signup_security_and_otp_rules(rules_setup):
    mgr = _login("07709000001")
    t2 = rules_setup["t2"]

    # 1. features.self_signup = False -> 403 SIGNUP_CLOSED
    mgr.patch("/api/v1/settings", {"advanced": {"features": {"self_signup": False}, "password_min": 8}}, format="json")
    closed = APIClient().post(
        "/api/v1/auth/register",
        {"name": "New Shop", "phone": "07809990001", "password": "Password123", "shop_name": "دكان", "area": "حي"},
        format="json",
    )
    assert closed.status_code == 403
    assert closed.json()["code"] == "SIGNUP_CLOSED"

    # 2. self_signup=True + auto_approve + custom tier + custom debt limit + password_min=8
    mgr.patch(
        "/api/v1/settings",
        {
            "advanced": {
                "features": {"self_signup": True},
                "password_min": 8,
                "signup_tier_id": t2.id,
                "signup_debt_limit": 150_000,
                "signup_auto_approve": True,
                "max_shops_per_account": 1,
                "login_lock_after": 2,
                "login_lock_minutes": 15,
            }
        },
        format="json",
    )
    short_pw = APIClient().post(
        "/api/v1/auth/register",
        {"name": "New Shop", "phone": "07809990001", "password": "short77", "shop_name": "دكان", "area": "حي"},
        format="json",
    )
    assert short_pw.status_code == 400
    assert "password" in short_pw.json()["errors"]

    ok_reg = APIClient().post(
        "/api/v1/auth/register",
        {"name": "Auto Owner", "phone": "07809990002", "password": "LongPass123", "shop_name": "متجر النور", "area": "الكرادة"},
        format="json",
    )
    assert ok_reg.status_code == 201, ok_reg.json()
    new_cust = Customer.objects.get(id=ok_reg.json()["data"]["user"]["customer_id"])
    assert new_cust.tier_id == t2.id
    assert new_cust.debt_limit == 150_000
    assert new_cust.active is True
    assert new_cust.status == CustomerStatus.ACTIVE

    # max_shops_per_account=1 blocks adding another shop
    shop_tok = ok_reg.json()["data"]["token"]
    sc = APIClient()
    sc.credentials(HTTP_AUTHORIZATION=f"Bearer {shop_tok}")
    extra = sc.post("/api/v1/me/shops", {"shop_name": "فرع ٢", "area": "حي"}, format="json")
    assert extra.status_code == 400

    # Login lock after 2 failed attempts -> 3rd attempt even with correct password returns 423 LOCKED
    anon = APIClient()
    assert anon.post("/api/v1/auth/login", {"phone": "07809990002", "password": "wrong1111"}, format="json").status_code == 401
    assert anon.post("/api/v1/auth/login", {"phone": "07809990002", "password": "wrong2222"}, format="json").status_code == 401
    locked = anon.post("/api/v1/auth/login", {"phone": "07809990002", "password": "LongPass123"}, format="json")
    assert locked.status_code == 423
    assert locked.json()["code"] == "LOCKED"

    # OTP dynamic tries limit: otp.tries = 2 -> 2nd wrong code locks challenge
    from django.test import override_settings

    with override_settings(WEVLIX_MODE="demo", DJANGO_ENV="development", ALLOW_DEMO_SEED=True):
        mgr.patch("/api/v1/settings", {"advanced": {"otp": {"tries": 2}}}, format="json")
        sent = APIClient().post("/api/v1/otp/send", {"phone": "07809990077", "purpose": "register"}, format="json")
        assert sent.status_code == 200
        rid = sent.json()["data"]["request_id"]
        ch = OtpChallenge.objects.get(request_id=rid)
        assert APIClient().post("/api/v1/otp/verify", {"request_id": rid, "code": "000000"}, format="json").status_code == 422
        ch.refresh_from_db()
        assert ch.attempts == 1
        assert APIClient().post("/api/v1/otp/verify", {"request_id": rid, "code": "000000"}, format="json").status_code == 429


@pytest.mark.django_db
def test_delivery_door_payment_custody_cap_reversals_visits_features_and_notify_matrix(rules_setup):
    mgr = _login("07709000001")
    acct = _login("07709000002")
    keeper = _login("07709000003")
    staff = _login("07709000008")
    rep = _login("07709000004")
    shop = _login("07709000005")

    wh = rules_setup["wh"]
    cust = rules_setup["cust"]
    item = rules_setup["item"]
    rep_u = rules_setup["rep"]
    shop_u = rules_setup["shop_u"]

    # Configure rules: pin_tries=2, photo_required=False, custody_cap=10000, visit_near_m=50, far_visit_alert=True,
    # reverse_roles=["manager"], reverse_days=1, notify_matrix override on payment
    mgr.patch(
        "/api/v1/settings",
        {
            "advanced": {
                "pin_tries": 2,
                "photo_required": False,
                "door_methods": ["cash"],
                "low_rating_stars": 3,
                "custody_cap": 10_000,
                "reverse_roles": ["manager"],
                "reverse_days": 1,
                "visit_near_m": 50,
                "far_visit_alert": True,
                "features": {
                    "online_pay": False,
                    "ratings": True,
                    "clearance": False,
                    "notify_me": False,
                    "support_chat": False,
                    "reorder": False,
                },
                "notify_matrix": {"payment": ["manager", "staff"]},
            }
        },
        format="json",
    )

    # Feature-off checks
    assert shop.post("/api/v1/payments/online", {"amount": 5000, "provider": "qi"}, format="json").status_code == 403
    assert shop.post("/api/v1/stock-alerts", {"item_id": item.id, "on": True}, format="json").status_code == 403
    assert shop.post("/api/v1/chat/me", {"text": "hi"}, format="json").status_code == 403
    assert mgr.post("/api/v1/clearances", {"batch_id": 1, "percent": 10}, format="json").status_code == 403
    assert shop.get("/api/v1/reorder/due").json()["data"] == []

    # Create order -> prepare -> dispatch -> test wrong pin_tries=2 -> unlock -> deliver without photo + cash door payment
    o = rep.post(
        "/api/v1/orders",
        {"customer_id": cust.id, "warehouse_id": wh.id, "lines": [{"item_id": item.id, "unit": "carton", "qty": 2}]},
        format="json",
    ).json()["data"]
    assert keeper.post(f"/api/v1/orders/{o['id']}/prepare", {"warehouses": {str(item.id): wh.id}, "rep_id": rep_u.id}, format="json").status_code == 200
    dlv = Delivery.objects.get(order_id=o["id"])
    assert rep.post("/api/v1/deliveries/dispatch", {"code": f"NAHRAIN:DLV:{dlv.code}"}, format="json").status_code == 200

    # pin_tries = 2
    p1 = rep.post(f"/api/v1/orders/{o['id']}/deliver", {"pin": "0000"}, format="json")
    assert p1.status_code == 200 and p1.json()["data"]["left"] == 1 and not p1.json()["data"]["locked"]
    p2 = rep.post(f"/api/v1/orders/{o['id']}/deliver", {"pin": "0000"}, format="json")
    assert p2.status_code == 200 and p2.json()["data"]["left"] == 0 and p2.json()["data"]["locked"]

    # Unlock + get shop PIN
    assert mgr.post(f"/api/v1/orders/{o['id']}/delivery/unlock", {}, format="json").status_code == 200
    real_pin = shop.get(f"/api/v1/orders/{o['id']}").json()["data"]["delivery"]["pin"]

    # Trying disabled door method "qi" -> 403 FEATURE_OFF
    bad_pay = rep.post(
        f"/api/v1/orders/{o['id']}/deliver",
        {"pin": real_pin, "payment": {"method": "qi", "amount": 5000}},
        format="json",
    )
    assert bad_pay.status_code == 403
    assert bad_pay.json()["code"] == "FEATURE_OFF"

    # Deliver WITHOUT photo (photo_required=False) + cash door payment of 15,000 (> custody_cap=10,000)
    ok_del = rep.post(
        f"/api/v1/orders/{o['id']}/deliver",
        {"pin": real_pin, "payment": {"method": "cash", "amount": 15_000}},
        format="json",
    )
    assert ok_del.status_code == 200, ok_del.json()
    order_out = ok_del.json()["data"]["order"]
    assert order_out["delivery"]["has_photo"] is False
    assert order_out["delivery"]["payment"]["method"] == "cash"
    assert order_out["delivery"]["payment"]["status"] == "paid"

    # notify_matrix["payment"] = ["manager", "staff"]:
    # shop_u got NO "payment" notification, staff got a role broadcast copy!
    assert Notification.objects.filter(user=shop_u, kind="payment").count() == 0
    assert Notification.objects.filter(target_role=Role.STAFF, kind="payment").count() == 1

    # custody_cap triggered ONCE per day for rep (second cash payment same day does NOT duplicate)
    assert Notification.objects.filter(kind="custody", ref_id=f"cap:{rep_u.id}:{baghdad_today().isoformat()}").count() == 2  # accountant + manager
    assert rep.post("/api/v1/payments", {"customer_id": cust.id, "amount": 2000, "method": "cash"}, format="json").status_code == 200
    assert Notification.objects.filter(kind="custody", ref_id=f"cap:{rep_u.id}:{baghdad_today().isoformat()}").count() == 2

    # Rating with low_rating_stars=3 alerts manager; then ratings=False blocks rating
    r_ok = shop.post(f"/api/v1/orders/{o['id']}/rating", {"stars": 3, "note": "بطيء"}, format="json")
    assert r_ok.status_code == 200
    assert Notification.objects.filter(target_role=Role.MANAGER, kind="needs_approval", ref_id=str(o["id"])).exists()
    mgr.patch(
        "/api/v1/settings",
        {
            "advanced": {
                "reverse_roles": ["manager"],
                "reverse_days": 1,
                "visit_near_m": 50,
                "far_visit_alert": True,
                "features": {"ratings": False},
            }
        },
        format="json",
    )
    assert shop.post(f"/api/v1/orders/{o['id']}/rating", {"stars": 4}, format="json").status_code == 403

    # Reverse role restriction (reverse_roles=["manager"]) -> 403 for accountant
    pay_id = Payment.objects.filter(customer=cust).order_by("id").first().id
    assert acct.post(f"/api/v1/payments/{pay_id}/reverse", {"reason": "test"}, format="json").status_code == 403

    # Also test reverse_days TOO_OLD when accountant IS allowed
    mgr.patch(
        "/api/v1/settings",
        {"advanced": {"reverse_roles": ["manager", "accountant"], "reverse_days": 1, "visit_near_m": 50, "far_visit_alert": True}},
        format="json",
    )
    Payment.objects.filter(id=pay_id).update(created_at=datetime.now(tz=UTC) - timedelta(days=3))
    too_old = acct.post(f"/api/v1/payments/{pay_id}/reverse", {"reason": "old"}, format="json")
    assert too_old.status_code == 403
    assert too_old.json()["code"] == "TOO_OLD"

    # Visit far alert (>50m -> far=True + manager notification)
    v_res = rep.post(
        "/api/v1/visits",
        {"customer_id": cust.id, "outcome": "no_order", "reason": "other", "lat": 32.605, "lng": 44.005},
        format="json",
    )
    assert v_res.status_code == 200
    assert v_res.json()["data"]["far"] is True
    assert Notification.objects.filter(target_role=Role.MANAGER, kind="needs_approval", ref_type="customer", ref_id=str(cust.id)).exists()


def test_rules_v2_validation_422(rules_setup):
    mgr = _login("07709000001")
    acct = rules_setup["acct"]

    # Duplicate document prefixes -> 422
    bad_pfx = mgr.patch(
        "/api/v1/settings",
        {"advanced": {"names": {"prefixes": {"order": "ORD", "invoice": "ORD"}}}},
        format="json",
    )
    assert bad_pfx.status_code == 422
    assert "names.prefixes" in bad_pfx.json()["errors"]

    # Invalid work hours (from >= to) -> 422
    bad_work = mgr.patch(
        "/api/v1/settings",
        {"advanced": {"work": {"on": True, "from": "17:00", "to": "08:00"}}},
        format="json",
    )
    assert bad_work.status_code == 422
    assert "work.hours" in bad_work.json()["errors"]

    # discount_by_role above global discount_max -> 422
    bad_dbr = mgr.patch(
        "/api/v1/settings",
        {"advanced": {"discount_max": 20, "discount_by_role": {"accountant": 25}}},
        format="json",
    )
    assert bad_dbr.status_code == 422
    assert "discount_by_role.accountant" in bad_dbr.json()["errors"]

    # people[uid].discount_max above global discount_max -> 422
    bad_ppl = mgr.patch(
        "/api/v1/settings",
        {"advanced": {"discount_max": 20, "people": {str(acct.id): {"discount_max": 25}}}},
        format="json",
    )
    assert bad_ppl.status_code == 422
    assert f"people.{acct.id}.discount_max" in bad_ppl.json()["errors"]


def test_rules_v2_permissions_people_redaction_and_manager_guard(rules_setup):
    mgr = _login("07709000001")
    acct = _login("07709000002")
    keeper = _login("07709000003")
    staff = _login("07709000008")
    rep = _login("07709000004")

    acct_u = rules_setup["acct"]
    keeper_u = rules_setup["keeper"]
    staff_u = rules_setup["staff"]
    mgr_u = rules_setup["mgr"]
    wh = rules_setup["wh"]
    cust = rules_setup["cust"]
    item = rules_setup["item"]

    # Default permissions: staff cannot edit items (403 "ليس من صلاحياتك"), acct cannot edit_items either
    item_payload = {
        "name": "شاي",
        "name_en": "Tea",
        "category": "مواد غذائية",
        "pieces_per_carton": 12,
        "prices": [{"tier_id": rules_setup["t1"].id, "carton_price": 12000, "piece_price": 1000}],
    }
    no_item = staff.post("/api/v1/items", item_payload, format="json")
    assert no_item.status_code == 403
    assert "ليس من صلاحياتك" in no_item.json()["message"]

    # Manager grants edit_items to role staff + approve_expense to acct, and tests deny/allow override on acct_u
    r_upd = mgr.patch(
        "/api/v1/settings",
        {
            "advanced": {
                "permissions": {
                    "edit_items": ["manager", "staff"],
                    "edit_customers": ["manager", "staff"],
                    "approve_shop": ["manager"],
                    "approve_expense": ["manager", "accountant"],
                    "manage_users": ["manager", "accountant"],
                    "see_costs": ["manager", "accountant"],
                    "see_debts": ["manager", "accountant"],
                },
                "people": {
                    str(staff_u.id): {"deny": ["edit_customers"], "allow": ["approve_shop"]},
                    str(acct_u.id): {"deny": ["see_costs", "see_debts"]},
                },
            }
        },
        format="json",
    )
    assert r_upd.status_code == 200

    # Staff can now create an item via role permission
    yes_item = staff.post("/api/v1/items", item_payload, format="json")
    assert yes_item.status_code == 201, yes_item.json()

    # Deny beats role for staff on edit_customers
    no_cust = staff.post(
        "/api/v1/customers",
        {"name": "محل جديد", "owner_name": "علي", "phone": "07709111222", "tier_id": rules_setup["t1"].id},
        format="json",
    )
    assert no_cust.status_code == 403

    # Allow beats role for staff on approve_shop
    pend_cust = Customer.objects.create(
        name="محل معلق",
        owner_name="كريم",
        phone="07709111223",
        tier=rules_setup["t1"],
        status=CustomerStatus.PENDING,
        active=False,
    )
    yes_app = staff.post(f"/api/v1/customers/{pend_cust.id}/reject", {"reason": "اختبار"}, format="json")
    assert yes_app.status_code == 200

    # Manager always allowed even if permissions stripped (keep people override so redaction check below holds)
    r_strip = mgr.patch(
        "/api/v1/settings",
        {
            "advanced": {
                "permissions": {
                    "edit_items": [],
                    "approve_expense": ["manager", "accountant"],
                    "manage_users": ["manager", "accountant"],
                    "see_costs": ["manager", "accountant"],
                    "see_debts": ["manager", "accountant"],
                },
                "people": {
                    str(acct_u.id): {"deny": ["see_costs", "see_debts"]},
                },
            }
        },
        format="json",
    )
    assert r_strip.status_code == 200
    mgr_item = mgr.post(
        "/api/v1/items",
        {
            "name": "سكر",
            "name_en": "Sugar",
            "category": "مواد غذائية",
            "pieces_per_carton": 10,
            "prices": [{"tier_id": rules_setup["t1"].id, "carton_price": 10000, "piece_price": 1000}],
        },
        format="json",
    )
    assert mgr_item.status_code == 201, mgr_item.json()

    # Personal deny on see_costs / see_debts hides cost fields from accountant on /items & /reports/debts & /dashboard
    items_res = acct.get("/api/v1/items")
    assert items_res.status_code == 200
    first_item = next(x for x in items_res.json()["data"] if x["id"] == item.id)
    assert "avg_cost" not in first_item
    dash_res = acct.get("/api/v1/dashboard")
    assert dash_res.status_code == 200
    assert dash_res.json()["data"]["total_debt"] == 0
    assert dash_res.json()["data"]["today_collected"] == 0
    assert "month_profit" not in dash_res.json()["data"]
    assert acct.get("/api/v1/reports/debts").status_code == 403
    cust_res = acct.get(f"/api/v1/customers/{cust.id}")
    assert cust_res.status_code == 200
    assert "balance" not in cust_res.json()["data"]

    # Accountant has manage_users, so can create/edit staff, but CANNOT create/edit/delete manager (403)
    ok_create = acct.post(
        "/api/v1/users",
        {"name": "Staff Two", "phone": "07709333444", "password": "Password123", "role": "staff", "warehouse_id": wh.id},
        format="json",
    )
    assert ok_create.status_code == 201
    new_uid = ok_create.json()["data"]["id"]
    assert acct.patch(f"/api/v1/users/{new_uid}", {"name": "Staff Two Edited"}, format="json").status_code == 200
    no_promote = acct.patch(f"/api/v1/users/{new_uid}", {"role": "manager"}, format="json")
    assert no_promote.status_code == 403
    no_mgr_create = acct.post(
        "/api/v1/users",
        {"name": "Fake Mgr", "phone": "07709333445", "password": "Password123", "role": "manager"},
        format="json",
    )
    assert no_mgr_create.status_code == 403
    assert acct.patch(f"/api/v1/users/{mgr_u.id}", {"name": "Hacked"}, format="json").status_code == 403
    assert acct.delete(f"/api/v1/users/{mgr_u.id}").status_code == 403

    # Warehouse guard still holds even if receive_goods granted
    wh2 = Warehouse.objects.create(name="مخزن ٢", name_en="WH2", code="W2")
    bad_wh_rec = keeper.post(
        "/api/v1/receipts",
        {
            "warehouse_id": wh2.id,
            "supplier_name": "مورد",
            "lines": [{"item_id": item.id, "qty": 10, "unit_cost": 800, "expiry_date": str(baghdad_today() + timedelta(days=90))}],
        },
        format="json",
    )
    assert bad_wh_rec.status_code == 403
    assert keeper_u.warehouse_id == wh.id

    # cancel_order: creator can cancel own open order without explicit permission; non-writer without permission cannot
    o_res = rep.post(
        "/api/v1/orders",
        {"customer_id": cust.id, "warehouse_id": wh.id, "lines": [{"item_id": item.id, "unit": "carton", "qty": 6}]},
        format="json",
    )
    assert o_res.status_code == 201, o_res.json()
    assert o_res.json()["data"]["status"] == OrderStatus.NEEDS_APPROVAL
    oid = o_res.json()["data"]["id"]
    assert rep.post(f"/api/v1/orders/{oid}/cancel", {"reason": "خطأ"}, format="json").status_code == 200

    # Non-writer (accountant lacks cancel_order by default in Round 2? No, acct has cancel_order in def, so check staff!)
    o2_res = rep.post(
        "/api/v1/orders",
        {"customer_id": cust.id, "warehouse_id": wh.id, "lines": [{"item_id": item.id, "unit": "carton", "qty": 1}]},
        format="json",
    )
    assert o2_res.status_code == 201, o2_res.json()
    oid2 = o2_res.json()["data"]["id"]
    # Strip cancel_order from all non-managers and confirm acct (non-writer) gets 403 while rep (writer while open) still gets 200
    mgr.patch("/api/v1/settings", {"advanced": {"permissions": {"cancel_order": ["manager"]}}}, format="json")
    assert acct.post(f"/api/v1/orders/{oid2}/cancel", {"reason": "بدون صلاحية"}, format="json").status_code == 403
    assert rep.post(f"/api/v1/orders/{oid2}/cancel", {"reason": "الكاتب يلغي"}, format="json").status_code == 200


def test_rules_v2_limits_work_delivery_warehouses_and_names(rules_setup, monkeypatch):
    mgr = _login("07709000001")
    acct = _login("07709000002")
    rep = _login("07709000004")

    acct_u = rules_setup["acct"]
    rep_u = rules_setup["rep"]
    wh = rules_setup["wh"]
    cust = rules_setup["cust"]
    item = rules_setup["item"]

    # Seed old order first to confirm prefixes only apply to new records
    old_o = rep.post(
        "/api/v1/orders",
        {"customer_id": cust.id, "warehouse_id": wh.id, "lines": [{"item_id": item.id, "unit": "carton", "qty": 1}]},
        format="json",
    )
    assert old_o.status_code == 201, old_o.json()
    old_num = old_o.json()["data"]["number"]
    assert old_num.startswith("ORD-")

    # 1. Configure limits (discount_by_role, expense_by_role, custody_cap, people), work, delivery_plan, warehouses, names
    current_dt = datetime(2026, 11, 15, 19, 30, tzinfo=baghdad_now().tzinfo)  # 19:30 Baghdad (after work hours)

    class _FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return current_dt.astimezone(tz) if tz is not None else current_dt.replace(tzinfo=None)

    monkeypatch.setattr("apps.accounts.business_rules.baghdad_now", lambda: current_dt)
    monkeypatch.setattr("apps.orders.services.datetime", _FakeDateTime)

    upd = mgr.patch(
        "/api/v1/settings",
        {
            "advanced": {
                "discount_max": 30,
                "discount_alone_max": 3,
                "discount_by_role": {"staff": 6},
                "custody_cap": 1_000_000,
                "expense_by_role": {"accountant": 25_000},
                "people": {
                    str(rep_u.id): {"discount_max": 5, "custody_cap": 200_000, "allow": ["give_discount"]},
                    str(acct_u.id): {"discount_max": 12, "expense_limit": 10_000},
                },
                "work": {
                    "on": True,
                    "days": [0, 1, 2, 3, 4],  # Sun-Thu
                    "from": "08:00",
                    "to": "17:00",
                    "holidays": [{"date": "2026-11-16", "name": "عطلة رسمية"}],  # Monday is a holiday!
                },
                "delivery_plan": {
                    "on": True,
                    "cutoff": "14:00",
                    "days_by_area": {"حي الحسين": [2, 4]},  # Tue & Thu (2026-11-17 Tue)
                },
                "warehouses": {
                    str(wh.id): {"reservation_hours": 10, "shop_orders": True, "counter_sale": False},
                },
                "permissions": {"manage_users": ["manager", "accountant"]},
                "names": {
                    "prefixes": {"order": "NORD", "invoice": "NINV", "payment": "NPAY", "expense": "NEXP"},
                    "currency_ar": "د.ع",
                    "currency_en": "IQD",
                    "invoice_footer": "شكراً لتعاملكم مع النهرين",
                },
            }
        },
        format="json",
    )
    assert upd.status_code == 200

    # 2. GET /site includes public live.names & live.users_page
    site_res = APIClient().get("/api/v1/site")
    assert site_res.status_code == 200
    live = site_res.json()["data"]["live"]
    assert live["names"]["currency_ar"] == "د.ع"
    assert live["names"]["invoice_footer"] == "شكراً لتعاملكم مع النهرين"
    assert live["users_page"] == {"roles": ["manager", "accountant"], "allow": [], "deny": []}

    # 3. After-hours reservation + delivery_plan expected_on + new prefix NORD- (old order unchanged!)
    new_o = rep.post(
        "/api/v1/orders",
        {"customer_id": cust.id, "warehouse_id": wh.id, "lines": [{"item_id": item.id, "unit": "carton", "qty": 6}]},
        format="json",
    )
    assert new_o.status_code == 201, new_o.json()
    ndata = new_o.json()["data"]
    assert ndata["number"].startswith("NORD-")
    assert Order.objects.get(id=old_o.json()["data"]["id"]).number == old_num
    assert ndata.get("after_hours") is not None
    # 2026-11-15 Sun 19:30 -> Mon 2026-11-16 is holiday -> next work start is Tue 2026-11-17 08:00 Baghdad + 10h WH reservation = 18:00 Baghdad
    after_local = datetime.fromisoformat(ndata["after_hours"].replace("Z", "+00:00")).astimezone(baghdad_now().tzinfo)
    assert after_local.date().isoformat() == "2026-11-17"
    assert after_local.hour == 8
    reserved_local = datetime.fromisoformat(ndata["reserved_until"].replace("Z", "+00:00")).astimezone(baghdad_now().tzinfo)
    assert reserved_local.date().isoformat() == "2026-11-17"
    assert reserved_local.hour == 18
    assert ndata.get("expected_on") == "2026-11-17"

    # 4. Personal discount_max: rep person limit is 5% (6% needs_approval, 5% stays approved)
    Customer.objects.filter(id=cust.id).update(debt_limit=500_000)
    disc_o = rep.post(
        "/api/v1/orders",
        {"customer_id": cust.id, "warehouse_id": wh.id, "lines": [{"item_id": item.id, "unit": "carton", "qty": 1}]},
        format="json",
    )
    assert disc_o.status_code == 201
    assert disc_o.json()["data"]["status"] == OrderStatus.APPROVED
    did = disc_o.json()["data"]["id"]
    d5 = rep.post(f"/api/v1/orders/{did}/discount", {"percent": 5}, format="json")
    assert d5.status_code == 200
    assert d5.json()["data"]["status"] == OrderStatus.APPROVED
    d6 = rep.post(f"/api/v1/orders/{did}/discount", {"percent": 6}, format="json")
    assert d6.status_code == 200
    assert d6.json()["data"]["status"] == OrderStatus.NEEDS_APPROVAL
    assert "discount" in d6.json()["data"]["approval_for"]

    # 5. Personal expense_limit for acct = 10_000 (< 25_000 role limit -> exceeding triggers pending & prefix NEXP-)
    exp_ok = acct.post(
        "/api/v1/expenses",
        {
            "category": "وقود",
            "amount": 15_000,
            "date": "2026-11-15",
            "method": "cash",
            "paid_to": "محطة وقود",
            "note": "توصيل",
        },
        format="json",
    )
    assert exp_ok.status_code == 201, exp_ok.json()
    assert exp_ok.json()["data"]["number"].startswith("NEXP-")
    assert exp_ok.json()["data"]["status"] == "pending"

    # 6. Counter sale disabled for wh -> 403 FEATURE_OFF
    keeper = _login("07709000003")
    cs_res = keeper.post(
        "/api/v1/counter-sales",
        {
            "client_ref": "cs-off-1",
            "warehouse_id": wh.id,
            "tier_id": rules_setup["t1"].id,
            "method": "cash",
            "lines": [{"item_id": item.id, "unit": "carton", "qty": 1}],
        },
        format="json",
    )
    assert cs_res.status_code == 403, cs_res.json()
    assert cs_res.json()["code"] == "FEATURE_OFF"

    # 7. Closed day (Monday holiday 2026-11-16) blocks delivery dispatch -> 409 CLOSED_DAY
    mgr.post(f"/api/v1/orders/{ndata['id']}/approve", {}, format="json")
    mgr.post(f"/api/v1/orders/{ndata['id']}/accept", {}, format="json")
    Order.objects.filter(id=ndata["id"]).update(status=OrderStatus.APPROVED, warehouse=wh)
    Delivery.objects.create(order_id=ndata["id"], rep=rep_u, code="CLOSED01", status=DeliveryStatus.READY)
    current_dt = datetime(2026, 11, 16, 10, 0, tzinfo=baghdad_now().tzinfo)
    monkeypatch.setattr("apps.deliveries.services.datetime", _FakeDateTime)
    disp_res = rep.post("/api/v1/deliveries/dispatch", {"code": "NAHRAIN:DLV:CLOSED01"}, format="json")
    assert disp_res.status_code == 409
    assert disp_res.json()["code"] == "CLOSED_DAY"

    # 8. Shop orders disabled on warehouse -> shop order blocked (409 BAD_STATE)
    mgr.patch(
        "/api/v1/settings",
        {"advanced": {"warehouses": {str(wh.id): {"reservation_hours": 10, "shop_orders": False, "counter_sale": True}}}},
        format="json",
    )
    shop = _login("07709000005")
    so_res = shop.post(
        "/api/v1/orders",
        {"customer_id": cust.id, "warehouse_id": wh.id, "lines": [{"item_id": item.id, "unit": "carton", "qty": 1}]},
        format="json",
    )
    assert so_res.status_code == 409
    assert so_res.json()["code"] == "BAD_STATE"

