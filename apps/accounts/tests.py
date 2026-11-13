"""Phase 2 tests: Auth, JWT revocation, shop registration, RBAC, users, settings, price tiers."""
from __future__ import annotations

import pytest
from rest_framework.test import APIClient

from apps.accounts.models import PriceTier, Role, SystemSetting, User
from apps.audit.models import AuditLog
from apps.customers.models import Customer, CustomerStatus, ShopMembership
from apps.notifications.models import Notification
from apps.warehouses.models import Warehouse


@pytest.fixture
def seed_base_infra(db):
    wh1 = Warehouse.objects.create(name="مخزن الرصافة", name_en="Rusafa Warehouse", code="RUS")
    wh2 = Warehouse.objects.create(name="مخزن الكرخ", name_en="Karkh Warehouse", code="KRK")
    tier1 = PriceTier.objects.create(name="جملة", name_en="Wholesale", sort_order=1)
    tier2 = PriceTier.objects.create(name="تجزئة", name_en="Retail", sort_order=2)
    SystemSetting.get_solo()
    manager = User.objects.create_user(phone="07701000001", password="Password123", name="Manager One", role=Role.MANAGER)
    accountant = User.objects.create_user(phone="07701000002", password="Password123", name="Accountant One", role=Role.ACCOUNTANT)
    keeper = User.objects.create_user(
        phone="07701000003",
        password="Password123",
        name="Keeper Rusafa",
        role=Role.WAREHOUSE,
        warehouse=wh1,
    )
    rep = User.objects.create_user(
        phone="07701000004",
        password="Password123",
        name="Sales Rep",
        role=Role.REP,
        rep_kinds=["sales"],
    )
    return {
        "wh1": wh1,
        "wh2": wh2,
        "tier1": tier1,
        "tier2": tier2,
        "manager": manager,
        "accountant": accountant,
        "keeper": keeper,
        "rep": rep,
    }


def _auth_client(phone: str, password: str = "Password123", lang: str = "en") -> tuple[APIClient, dict]:
    client = APIClient()
    res = client.post("/api/v1/auth/login", {"phone": phone, "password": password}, format="json", HTTP_ACCEPT_LANGUAGE=lang)
    assert res.status_code == 200, res.json()
    body = res.json()
    assert body["success"] is True
    token = body["data"]["token"]
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
    return client, body["data"]


@pytest.mark.django_db
def test_login_phone_variants_and_bilingual_errors(seed_base_infra):
    client = APIClient()

    # Normalized login via +964 and Arabic-Indic numerals
    res = client.post("/api/v1/auth/login", {"phone": "+964 770 100 0001", "password": "Password123"}, format="json")
    assert res.status_code == 200
    assert res.json()["data"]["user"]["phone"] == "07701000001"

    res_ar_digits = client.post("/api/v1/auth/login", {"phone": "٠٧٧٠١٠٠٠٠٠١", "password": "Password123"}, format="json")
    assert res_ar_digits.status_code == 200

    # Wrong password in Arabic vs English
    bad_ar = client.post("/api/v1/auth/login", {"phone": "07701000001", "password": "wrong"}, format="json", HTTP_ACCEPT_LANGUAGE="ar")
    assert bad_ar.status_code == 401
    assert bad_ar.json()["code"] == "UNAUTHENTICATED"
    assert "غير صحيحة" in bad_ar.json()["message"]

    bad_en = client.post("/api/v1/auth/login", {"phone": "07701000001", "password": "wrong"}, format="json", HTTP_ACCEPT_LANGUAGE="en")
    assert bad_en.status_code == 401
    assert bad_en.json()["code"] == "UNAUTHENTICATED"
    assert "Invalid" in bad_en.json()["message"]


@pytest.mark.django_db
def test_logout_revokes_access_and_refresh_tokens(seed_base_infra):
    client, session = _auth_client("07701000001")
    assert client.get("/api/v1/me").status_code == 200

    out = client.post("/api/v1/auth/logout", {"refresh": session["refresh"]}, format="json")
    assert out.status_code == 200
    assert out.json()["data"]["ok"] is True

    # Access token must immediately fail
    assert client.get("/api/v1/me").status_code == 401

    # Refresh token must also fail
    ref = client.post("/api/v1/auth/refresh", {"refresh": session["refresh"]}, format="json")
    assert ref.status_code == 401


@pytest.mark.django_db
def test_refresh_rotation_and_deactivation_invalidation(seed_base_infra):
    manager_client, _ = _auth_client("07701000001")
    rep_client, rep_session = _auth_client("07701000004")

    # Deactivate rep via manager
    upd = manager_client.patch(f"/api/v1/users/{seed_base_infra['rep'].id}", {"active": False}, format="json")
    assert upd.status_code == 200

    # Existing rep JWT rejected immediately
    assert rep_client.get("/api/v1/me").status_code == 401
    # Inactive user cannot log in
    bad_login = APIClient().post("/api/v1/auth/login", {"phone": "07701000004", "password": "Password123"}, format="json")
    assert bad_login.status_code == 403
    assert bad_login.json()["code"] == "FORBIDDEN"

    # Refresh also rejected
    assert APIClient().post("/api/v1/auth/refresh", {"refresh": rep_session["refresh"]}, format="json").status_code == 401


@pytest.mark.django_db
def test_shop_registration_creates_pending_customer_and_notifications(seed_base_infra):
    client = APIClient()
    res = client.post(
        "/api/v1/auth/register",
        {
            "name": "Ali Hassan",
            "phone": "07801234567",
            "password": "secret99",
            "shop_name": "أسواق دجلة",
            "city": "بغداد",
            "area": "الكرادة",
            "street": "شارع 62",
            "landmark": "قرب ساحة الواثق",
            "lat": 33.3012,
            "lng": 44.4321,
        },
        format="json",
    )
    assert res.status_code == 201, res.json()
    data = res.json()["data"]
    assert data["user"]["role"] == Role.SHOP
    assert data["user"]["customer_id"] is not None

    customer = Customer.objects.get(id=data["user"]["customer_id"])
    assert customer.status == CustomerStatus.PENDING
    assert customer.debt_limit == 0
    assert ShopMembership.objects.filter(customer=customer, is_active_shop=True).count() == 1
    assert Notification.objects.filter(kind="shop_registered").count() == 2
    assert AuditLog.objects.filter(action="shop.register", entity_id=str(customer.id)).exists()

    # Duplicate phone blocked
    dup = client.post(
        "/api/v1/auth/register",
        {
            "name": "Duplicate",
            "phone": "+9647801234567",
            "password": "secret99",
            "shop_name": "Test",
            "city": "Baghdad",
            "area": "Karrada",
            "street": "Main",
            "landmark": "Park",
        },
        format="json",
    )
    assert dup.status_code == 400
    assert dup.json()["code"] == "VALIDATION"


@pytest.mark.django_db
def test_single_active_warehouse_keeper_and_rbac(seed_base_infra):
    wh1 = seed_base_infra["wh1"]
    manager_client, _ = _auth_client("07701000001")
    acct_client, _ = _auth_client("07701000002")
    rep_client, _ = _auth_client("07701000004")

    # Users directory is manager-only (parity with admin UI)
    assert acct_client.get("/api/v1/users").status_code == 403
    assert rep_client.get("/api/v1/users").status_code == 403
    assert acct_client.patch("/api/v1/settings", {"usd_rate": 1520}, format="json").status_code == 403

    # Creating second active keeper for wh1 is rejected
    second_keeper = manager_client.post(
        "/api/v1/users",
        {
            "name": "Keeper Two",
            "phone": "07709990001",
            "password": "Password123",
            "role": Role.WAREHOUSE,
            "warehouse_id": wh1.id,
            "active": True,
        },
        format="json",
    )
    assert second_keeper.status_code == 400
    assert second_keeper.json()["code"] == "VALIDATION"

    # Multiple staff in same warehouse IS allowed
    staff_res = manager_client.post(
        "/api/v1/users",
        {
            "name": "Staff One",
            "phone": "07709990002",
            "password": "Password123",
            "role": Role.STAFF,
            "warehouse_id": wh1.id,
        },
        format="json",
    )
    assert staff_res.status_code == 201

    # Settings & price-tiers updates by manager
    s_res = manager_client.patch("/api/v1/settings", {"reservation_hours": 36, "usd_rate": 1510}, format="json")
    assert s_res.status_code == 200
    assert s_res.json()["data"]["reservation_hours"] == 36
    assert s_res.json()["data"]["usd_rate"] == 1510

    t_res = manager_client.post("/api/v1/price-tiers", {"name": "كبار التجار", "name_en": "VIP Wholesale"}, format="json")
    assert t_res.status_code == 201
    tier_id = t_res.json()["data"]["id"]
    assert manager_client.patch(f"/api/v1/price-tiers/{tier_id}", {"name_en": "Key Accounts"}, format="json").status_code == 200

    # Lookups hides reps for shop role, shows for manager
    lk_mgr = manager_client.get("/api/v1/lookups")
    assert lk_mgr.status_code == 200
    assert "reps" in lk_mgr.json()["data"]
    assert len(lk_mgr.json()["data"]["warehouses"]) == 2
