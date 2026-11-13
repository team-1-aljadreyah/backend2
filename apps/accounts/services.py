"""Business services for auth, shop registration, user CRUD, settings, and price tiers."""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from django.apps import apps
from django.db import IntegrityError, transaction
from django.db.models import Prefetch
from django.utils import timezone
from rest_framework import status as http_status

from apps.accounts.authentication import issue_tokens_for_user
from apps.accounts.health import is_weak_password
from apps.accounts.models import LoginAttempt, PriceTier, Role, SiteConfig, SystemSetting, User, UserSession
from apps.accounts.profiles import in_hours, profile_of
from apps.accounts.serializers import (
    serialize_settings,
    serialize_tier,
    serialize_user,
    serialize_vehicle,
    serialize_warehouse,
    validate_rep_kinds,
)
from apps.audit.services import record_audit
from apps.common.exceptions import ApiException, ErrorCode
from apps.common.utils import client_ip, decode_image_data_url, normalize_phone
from apps.customers.models import Customer, CustomerStatus, ShopMembership
from apps.notifications.services import notify_role
from apps.warehouses.models import Warehouse
from integrations.storage import get_storage


def _validate_nonempty_str(data: dict[str, Any], key: str, ar_label: str, en_label: str) -> str:
    val = str(data.get(key) or "").strip()
    if not val:
        msg = (f"{ar_label} مطلوب", f"{en_label} is required")
        raise ApiException(code=ErrorCode.VALIDATION, message=msg, errors={key: msg})
    return val


def login_with_phone(raw_phone: str, password: str, *, request=None) -> dict[str, Any]:
    phone = normalize_phone(raw_phone)
    if not password:
        msg = ("كلمة المرور مطلوبة", "Password is required")
        raise ApiException(code=ErrorCode.VALIDATION, message=msg, errors={"password": msg})

    ip = client_ip(request)
    user = User.objects.filter(phone=phone, deleted=False).first()
    if not user or not user.check_password(password):
        LoginAttempt.objects.create(phone=phone, user=user, ok=False, reason="wrong", ip_address=ip)
        raise ApiException(
            code=ErrorCode.UNAUTHENTICATED,
            message=("رقم الهاتف أو كلمة المرور غير صحيحة", "Invalid phone or password"),
            status_code=http_status.HTTP_401_UNAUTHORIZED,
        )
    if not user.active:
        LoginAttempt.objects.create(phone=phone, user=user, ok=False, reason="stopped", ip_address=ip)
        raise ApiException(
            code=ErrorCode.FORBIDDEN,
            message=("تم إيقاف هذا الحساب. راجع الإدارة", "This account has been disabled"),
            status_code=http_status.HTTP_403_FORBIDDEN,
        )

    prof = profile_of(user)
    if prof and not in_hours(prof.get("hours")):
        h = prof["hours"]
        LoginAttempt.objects.create(phone=phone, user=user, ok=False, reason="hours", ip_address=ip)
        raise ApiException(
            code=ErrorCode.FORBIDDEN,
            message=(
                f"خارج ساعات الدخول المسموحة لك ({h.get('from')} – {h.get('to')})",
                f"Outside your login hours ({h.get('from')}–{h.get('to')})",
            ),
            status_code=http_status.HTTP_403_FORBIDDEN,
        )

    weak_now = is_weak_password(password, user.phone)
    update_fields = ["last_login"]
    user.last_login = datetime.now(tz=UTC)
    if user.weak_password != weak_now:
        user.weak_password = weak_now
        update_fields.append("weak_password")
    user.save(update_fields=update_fields)
    LoginAttempt.objects.create(phone=phone, user=user, ok=True, reason="", ip_address=ip)
    access, refresh = issue_tokens_for_user(user, request=request)
    return {
        "token": access,
        "refresh": refresh,
        "user": serialize_user(user, request=request),
    }


@transaction.atomic
def register_shop_account(data: dict[str, Any], *, request=None) -> dict[str, Any]:
    from apps.customers.services import clean_shop_address

    name = str(data.get("owner_name") or data.get("name") or "").strip()
    if not name:
        msg = ("الاسم الكامل مطلوب", "Owner name is required")
        raise ApiException(code=ErrorCode.VALIDATION, message=msg, errors={"owner_name": msg})
    phone = normalize_phone(data.get("phone"))
    password = str(data.get("password") or "")
    if len(password) < 6:
        msg = ("كلمة المرور يجب أن تكون 6 أحرف على الأقل", "Password must be at least 6 characters")
        raise ApiException(code=ErrorCode.VALIDATION, message=msg, errors={"password": msg})

    shop_name = _validate_nonempty_str(data, "shop_name", "اسم المحل", "Shop name")
    area = _validate_nonempty_str(data, "area", "المنطقة", "Area")
    address_info = None
    if "address_info" in data and data.get("address_info") is not None:
        address_info = clean_shop_address(data["address_info"])

    raw_addr = str(data.get("address") or "").strip()
    city = str(data.get("city") or "").strip() or "بغداد"
    street = str(data.get("street") or "").strip() or (address_info or {}).get("street") or raw_addr or area
    landmark = str(data.get("landmark") or "").strip() or (address_info or {}).get("landmark") or raw_addr or area
    address_text = raw_addr or (address_info or {}).get("details") or f"{area} - {street}"

    if User.objects.filter(phone=phone).exists() or Customer.objects.filter(phone=phone).exists():
        msg = ("رقم الهاتف مسجل مسبقاً", "This phone number is already registered")
        raise ApiException(code=ErrorCode.VALIDATION, message=msg, errors={"phone": msg})

    lat = data.get("lat") if data.get("lat") not in (None, "") else (address_info or {}).get("lat")
    lng = data.get("lng") if data.get("lng") not in (None, "") else (address_info or {}).get("lng")
    try:
        lat_dec = Decimal(str(lat)) if lat is not None and lat != "" else None
        lng_dec = Decimal(str(lng)) if lng is not None and lng != "" else None
    except InvalidOperation as exc:
        raise ApiException(
            code=ErrorCode.VALIDATION,
            message=("إحداثيات الموقع غير صالحة", "Invalid location coordinates"),
        ) from exc

    default_tier = PriceTier.objects.order_by("sort_order", "id").first()
    if not default_tier:
        default_tier = PriceTier.objects.create(name="جملة", name_en="Wholesale", sort_order=1)

    customer = Customer.objects.create(
        name=shop_name,
        owner_name=name,
        phone=phone,
        city=city,
        area=area,
        street=street,
        landmark=landmark,
        address_text=address_text[:255],
        address_info=address_info,
        lat=lat_dec,
        lng=lng_dec,
        tier=default_tier,
        debt_limit=0,
        active=False,
        status=CustomerStatus.PENDING,
    )
    user = User.objects.create_user(
        phone=phone,
        password=password,
        name=name,
        role=Role.SHOP,
        active=True,
        weak_password=is_weak_password(password, phone),
    )
    ShopMembership.objects.create(
        user=user,
        customer=customer,
        is_primary=True,
        is_active_shop=True,
    )

    notify_role(
        role=Role.MANAGER,
        kind="shop_registered",
        title_ar=f"طلب تسجيل محل جديد: {shop_name}",
        title_en=f"New shop registration pending: {shop_name}",
        ref_type="customer",
        ref_id=customer.id,
    )
    notify_role(
        role=Role.ACCOUNTANT,
        kind="shop_registered",
        title_ar=f"طلب تسجيل محل جديد: {shop_name}",
        title_en=f"New shop registration pending: {shop_name}",
        ref_type="customer",
        ref_id=customer.id,
    )
    record_audit(
        actor=user,
        action="shop.register",
        entity_type="customer",
        entity_id=customer.id,
        summary=f"Shop {shop_name} registered ({phone})",
        after={"customer_id": customer.id, "status": customer.status},
    )

    access, refresh = issue_tokens_for_user(user, active_customer_id=customer.id, request=request)
    return {
        "token": access,
        "refresh": refresh,
        "user": serialize_user(user, active_customer_id=customer.id, customer_ids=[customer.id]),
    }


def list_all_users() -> list[dict[str, Any]]:
    all_memberships = ShopMembership.objects.order_by("id").only("user_id", "customer_id", "is_active_shop")
    users = (
        User.objects.filter(deleted=False)
        .prefetch_related(Prefetch("shop_memberships", queryset=all_memberships, to_attr="_all_ms"))
        .order_by("id")
    )
    out = []
    for u in users:
        ms_list = getattr(u, "_all_ms", [])
        cids = [m.customer_id for m in ms_list]
        active_m = next((m for m in ms_list if m.is_active_shop), None)
        cid = active_m.customer_id if active_m else (cids[0] if cids else None)
        out.append(serialize_user(u, active_customer_id=cid, customer_ids=cids))
    return out


def _validate_user_role_fields(
    *,
    role: str,
    active: bool,
    warehouse_id: int | None,
    rep_kinds: list[str],
    rep_categories: list[str],
    exclude_user_id: int | None = None,
) -> tuple[Warehouse | None, list[str], list[str]]:
    if role not in Role.values:
        raise ApiException(code=ErrorCode.VALIDATION, message=("الدور غير صحيح", "Invalid user role"))

    wh: Warehouse | None = None
    if role in (Role.WAREHOUSE, Role.STAFF):
        if not warehouse_id:
            msg = ("يجب اختيار المخزن", "Warehouse assignment is required for warehouse/staff users")
            raise ApiException(code=ErrorCode.VALIDATION, message=msg, errors={"warehouse_id": msg})
        wh = Warehouse.objects.filter(id=warehouse_id, active=True).first()
        if not wh:
            raise ApiException(
                code=ErrorCode.VALIDATION,
                message=("المخزن المحدد غير موجود", "Selected warehouse not found"),
                errors={"warehouse_id": ("المخزن غير موجود", "Warehouse not found")},
            )
        if role == Role.WAREHOUSE and active:
            conflict = User.objects.filter(role=Role.WAREHOUSE, warehouse=wh, active=True, deleted=False)
            if exclude_user_id is not None:
                conflict = conflict.exclude(id=exclude_user_id)
            if conflict.exists():
                msg = (
                    f"يوجد أمين مخزن نشط بالفعل في {wh.name}",
                    f"An active warehouse keeper already exists for {wh.name_en}",
                )
                raise ApiException(code=ErrorCode.VALIDATION, message=msg, errors={"warehouse_id": msg})
    else:
        warehouse_id = None

    if role == Role.REP:
        try:
            kinds = validate_rep_kinds(rep_kinds)
        except ValueError as exc:
            raise ApiException(code=ErrorCode.VALIDATION, message=("نوع المندوب غير صحيح", str(exc))) from exc
        cats = [str(c).strip() for c in (rep_categories or []) if str(c).strip()]
    else:
        kinds = []
        cats = []

    return wh, kinds, cats


@transaction.atomic
def create_user_by_manager(*, actor: User, data: dict[str, Any]) -> dict[str, Any]:
    name = _validate_nonempty_str(data, "name", "الاسم", "Name")
    phone = normalize_phone(data.get("phone"))
    password = str(data.get("password") or "")
    if len(password) < 6:
        msg = ("كلمة المرور يجب أن تكون 6 أحرف على الأقل", "Password must be at least 6 characters")
        raise ApiException(code=ErrorCode.VALIDATION, message=msg, errors={"password": msg})

    if User.objects.filter(phone=phone).exists():
        msg = ("رقم الهاتف مستخدم مسبقاً", "Phone number already in use")
        raise ApiException(code=ErrorCode.VALIDATION, message=msg, errors={"phone": msg})

    role = str(data.get("role") or "")
    active = bool(data.get("active", True))
    sales_target = None
    if role == Role.REP and data.get("sales_target") not in (None, ""):
        try:
            st = int(data["sales_target"])
            if st < 0 or st > 1_000_000_000:
                raise ValueError
            sales_target = st
        except (TypeError, ValueError) as exc:
            msg = ("هدف المبيعات غير صحيح", "Invalid sales target")
            raise ApiException(code=ErrorCode.VALIDATION, message=msg, errors={"sales_target": msg}) from exc

    wh, kinds, cats = _validate_user_role_fields(
        role=role,
        active=active,
        warehouse_id=data.get("warehouse_id"),
        rep_kinds=data.get("rep_kinds") or [],
        rep_categories=data.get("rep_categories") or [],
    )
    try:
        user = User.objects.create_user(
            phone=phone,
            password=password,
            name=name,
            role=role,
            active=active,
            warehouse=wh,
            rep_kinds=kinds,
            rep_categories=cats,
            sales_target=sales_target,
            weak_password=is_weak_password(password, phone),
        )
    except IntegrityError as exc:
        raise ApiException(
            code=ErrorCode.VALIDATION,
            message=("تعذر إنشاء المستخدم بسبب تعارض بالبيانات", "User constraint conflict"),
        ) from exc

    record_audit(
        actor=actor,
        action="user.create",
        entity_type="user",
        entity_id=user.id,
        summary=f"Created user {user.name} ({user.role})",
        after=serialize_user(user),
    )
    return serialize_user(user)


@transaction.atomic
def update_user_by_manager(*, actor: User, user_id: int, data: dict[str, Any]) -> dict[str, Any]:
    target = User.objects.select_for_update().filter(id=user_id, deleted=False).first()
    if not target:
        raise ApiException(code=ErrorCode.NOT_FOUND, status_code=http_status.HTTP_404_NOT_FOUND)

    before = serialize_user(target)
    if "name" in data:
        target.name = _validate_nonempty_str(data, "name", "الاسم", "Name")
    if "phone" in data:
        new_phone = normalize_phone(data.get("phone"))
        if new_phone != target.phone and User.objects.filter(phone=new_phone).exclude(id=target.id).exists():
            msg = ("رقم الهاتف مستخدم مسبقاً", "Phone number already in use")
            raise ApiException(code=ErrorCode.VALIDATION, message=msg, errors={"phone": msg})
        target.phone = new_phone

    new_role = str(data.get("role", target.role))
    new_active = bool(data.get("active", target.active))
    if target.id == actor.id and not new_active:
        raise ApiException(
            code=ErrorCode.BAD_STATE,
            message=("لا يمكنك تعطيل حسابك الحالي", "You cannot deactivate your own active account"),
        )

    new_wh_id = data.get("warehouse_id", target.warehouse_id)
    new_kinds = data.get("rep_kinds", target.rep_kinds)
    new_cats = data.get("rep_categories", target.rep_categories)

    wh, kinds, cats = _validate_user_role_fields(
        role=new_role,
        active=new_active,
        warehouse_id=new_wh_id,
        rep_kinds=new_kinds,
        rep_categories=new_cats,
        exclude_user_id=target.id,
    )

    role_changed = new_role != target.role
    bump_token_version = role_changed or (not new_active and target.active)
    target.role = new_role
    target.active = new_active
    target.warehouse = wh
    target.rep_kinds = kinds
    target.rep_categories = cats
    if role_changed:
        target.profile_id = None
    if "sales_target" in data:
        if new_role == Role.REP and data["sales_target"] not in (None, ""):
            try:
                st = int(data["sales_target"])
                if st < 0 or st > 1_000_000_000:
                    raise ValueError
                target.sales_target = st
            except (TypeError, ValueError) as exc:
                msg = ("هدف المبيعات غير صحيح", "Invalid sales target")
                raise ApiException(code=ErrorCode.VALIDATION, message=msg, errors={"sales_target": msg}) from exc
        else:
            target.sales_target = None

    if data.get("password"):
        pw = str(data["password"])
        if len(pw) < 6:
            msg = ("كلمة المرور يجب أن تكون 6 أحرف على الأقل", "Password must be at least 6 characters")
            raise ApiException(code=ErrorCode.VALIDATION, message=msg, errors={"password": msg})
        target.set_password(pw)
        target.weak_password = is_weak_password(pw, target.phone)
        bump_token_version = True

    if bump_token_version:
        target.token_version += 1

    try:
        target.save()
    except IntegrityError as exc:
        raise ApiException(
            code=ErrorCode.VALIDATION,
            message=("تعذر تحديث المستخدم بسبب تعارض بالبيانات", "User constraint conflict"),
        ) from exc

    record_audit(
        actor=actor,
        action="user.update",
        entity_type="user",
        entity_id=target.id,
        summary=f"Updated user {target.name}",
        before=before,
        after=serialize_user(target),
    )
    return serialize_user(target)


@transaction.atomic
def update_settings_by_manager(*, actor: User, data: dict[str, Any]) -> dict[str, Any]:
    s = SystemSetting.objects.select_for_update().filter(pk=1).first() or SystemSetting.get_solo()
    before = serialize_settings(s)

    if "reservation_hours" in data:
        try:
            rh = int(data["reservation_hours"])
            if rh < 1 or rh > 720:
                raise ValueError
        except (TypeError, ValueError) as exc:
            raise ApiException(code=ErrorCode.VALIDATION, message=("ساعات الحجز غير صحيحة", "Invalid reservation hours")) from exc
        s.reservation_hours = rh

    if "near_expiry_days" in data:
        try:
            ned = int(data["near_expiry_days"])
            if ned < 1 or ned > 365:
                raise ValueError
        except (TypeError, ValueError) as exc:
            raise ApiException(code=ErrorCode.VALIDATION, message=("أيام قرب الانتهاء غير صحيحة", "Invalid near-expiry days")) from exc
        s.near_expiry_days = ned

    if "slow_moving_days" in data:
        try:
            smd = int(data["slow_moving_days"])
            if smd < 1 or smd > 365:
                raise ValueError
        except (TypeError, ValueError) as exc:
            raise ApiException(code=ErrorCode.VALIDATION, message=("أيام الركود غير صحيحة", "Invalid slow-moving days")) from exc
        s.slow_moving_days = smd

    if "auto_confirm_rep_orders" in data:
        s.auto_confirm_rep_orders = bool(data["auto_confirm_rep_orders"])

    if "commission_pct" in data:
        try:
            pct = Decimal(str(data["commission_pct"]))
            if pct < 0 or pct > 50:
                raise ValueError
        except (InvalidOperation, ValueError) as exc:
            raise ApiException(code=ErrorCode.VALIDATION, message=("نسبة العمولة غير صحيحة", "Invalid commission percentage")) from exc
        s.commission_pct = pct

    usd_changed = False
    if "usd_rate" in data:
        try:
            rate = Decimal(str(data["usd_rate"]))
            if rate <= 0:
                raise ValueError
        except (InvalidOperation, ValueError) as exc:
            raise ApiException(code=ErrorCode.VALIDATION, message=("سعر الصرف غير صحيح", "Invalid USD exchange rate")) from exc
        if rate != s.usd_rate:
            usd_changed = True
            s.usd_rate = rate

    if "phone" in data:
        s.phone = str(data.get("phone") or "").strip()[:40]
    if "address" in data:
        s.address = str(data.get("address") or "").strip()[:255]
    if "expense_approval_above" in data:
        raw_lim = data.get("expense_approval_above")
        if raw_lim is None or raw_lim == "":
            s.expense_approval_above = None
        else:
            try:
                lim = int(raw_lim)
                if lim < 0 or lim > 1_000_000_000:
                    raise ValueError
            except (TypeError, ValueError) as exc:
                raise ApiException(
                    code=ErrorCode.VALIDATION,
                    message=("حد موافقة الصرفيات غير صحيح", "Invalid expense approval limit"),
                ) from exc
            s.expense_approval_above = lim

    s.save()
    after = serialize_settings(s)

    if usd_changed and apps.is_installed("apps.exchange_rates"):
        from apps.common.utils import baghdad_today
        from apps.exchange_rates.models import UsdRate

        UsdRate.objects.update_or_create(date=baghdad_today(), defaults={"rate": s.usd_rate, "by": actor})

    record_audit(
        actor=actor,
        action="settings.update",
        entity_type="settings",
        entity_id=1,
        summary="Updated system settings",
        before=before,
        after=after,
    )
    return after


@transaction.atomic
def create_price_tier(*, actor: User, data: dict[str, Any]) -> dict[str, Any]:
    name = _validate_nonempty_str(data, "name", "اسم الفئة بالعربية", "Arabic tier name")
    name_en = _validate_nonempty_str(data, "name_en", "اسم الفئة بالإنجليزية", "English tier name")
    tier = PriceTier.objects.create(name=name, name_en=name_en, sort_order=PriceTier.objects.count() + 1)

    if apps.is_installed("apps.catalog"):
        from apps.catalog.models import Item, ItemPrice

        base_tier = PriceTier.objects.exclude(id=tier.id).order_by("sort_order", "id").first()
        for item in Item.objects.all():
            base_price = ItemPrice.objects.filter(item=item, tier=base_tier).first() if base_tier else None
            ItemPrice.objects.get_or_create(
                item=item,
                tier=tier,
                defaults={
                    "carton_price": base_price.carton_price if base_price else 0,
                    "piece_price": base_price.piece_price if base_price else 0,
                },
            )

    record_audit(
        actor=actor,
        action="price_tier.create",
        entity_type="price_tier",
        entity_id=tier.id,
        summary=f"Created price tier {tier.name_en}",
        after=serialize_tier(tier),
    )
    return serialize_tier(tier)


@transaction.atomic
def update_price_tier(*, actor: User, tier_id: int, data: dict[str, Any]) -> dict[str, Any]:
    tier = PriceTier.objects.filter(id=tier_id).first()
    if not tier:
        raise ApiException(code=ErrorCode.NOT_FOUND, status_code=http_status.HTTP_404_NOT_FOUND)
    before = serialize_tier(tier)
    if "name" in data:
        tier.name = _validate_nonempty_str(data, "name", "اسم الفئة بالعربية", "Arabic tier name")
    if "name_en" in data:
        tier.name_en = _validate_nonempty_str(data, "name_en", "اسم الفئة بالإنجليزية", "English tier name")
    tier.save()
    after = serialize_tier(tier)
    record_audit(
        actor=actor,
        action="price_tier.update",
        entity_type="price_tier",
        entity_id=tier.id,
        summary=f"Updated price tier {tier.name_en}",
        before=before,
        after=after,
    )
    return after


def build_lookups(user: User) -> dict[str, Any]:
    warehouses = [serialize_warehouse(w) for w in Warehouse.objects.filter(active=True).order_by("id")]
    tiers = [serialize_tier(t) for t in PriceTier.objects.order_by("sort_order", "id")]
    settings_obj = serialize_settings(SystemSetting.get_solo())

    categories: list[str] = []
    if apps.is_installed("apps.catalog"):
        from apps.catalog.models import Category, Item

        seen: set[str] = set()
        for c in Category.objects.order_by("sort_order", "id").values_list("name", flat=True):
            if c and c not in seen:
                seen.add(c)
                categories.append(c)
        for c in Item.objects.values_list("category", flat=True).distinct():
            if c and c not in seen:
                seen.add(c)
                categories.append(c)

    latest_usd = None
    if apps.is_installed("apps.exchange_rates"):
        from apps.exchange_rates.models import UsdRate
        from apps.exchange_rates.services import serialize_usd_rate

        row = UsdRate.objects.select_related("by").order_by("-date", "-at").first()
        if row:
            latest_usd = serialize_usd_rate(row)

    vehicles: list[dict[str, Any]] = []
    if apps.is_installed("apps.deliveries"):
        from apps.deliveries.models import Vehicle

        vehicles = [serialize_vehicle(v) for v in Vehicle.objects.filter(active=True).order_by("id")]

    payload: dict[str, Any] = {
        "warehouses": warehouses,
        "tiers": tiers,
        "categories": categories,
        "settings": settings_obj,
        "usd_rate": latest_usd,
        "vehicles": vehicles,
        "reps": [
            {
                "id": u.id,
                "name": u.name,
                "phone": u.phone,
                "kinds": list(u.rep_kinds or []),
                "rep_kinds": list(u.rep_kinds or []),
                "categories": list(u.rep_categories or []),
                **({"sales_target": int(u.sales_target)} if u.sales_target is not None else {}),
            }
            for u in User.objects.filter(role=Role.REP, active=True, deleted=False).order_by("id")
        ],
    }
    return payload


@transaction.atomic
def set_user_profile(*, actor: User, user_id: int, profile_id: str | None) -> dict[str, Any]:
    target = User.objects.select_for_update().filter(id=user_id, deleted=False).first()
    if not target:
        raise ApiException(code=ErrorCode.NOT_FOUND, status_code=http_status.HTTP_404_NOT_FOUND)
    if target.role == Role.MANAGER and profile_id:
        raise ApiException(
            code=ErrorCode.BAD_STATE,
            message=("لا يُقيَّد حساب المدير بملف صلاحيات", "Managers cannot be restricted by a profile"),
            status_code=http_status.HTTP_409_CONFLICT,
        )

    before = serialize_user(target)
    if profile_id:
        cfg = SiteConfig.objects.only("profiles").filter(pk=1).first()
        profiles = cfg.profiles if cfg and isinstance(cfg.profiles, list) else []
        prof = next((p for p in profiles if isinstance(p, dict) and p.get("id") == profile_id), None)
        if not prof or prof.get("role") != target.role:
            raise ApiException(
                code=ErrorCode.VALIDATION,
                message=("ملف الصلاحيات لا يناسب هذا المستخدم", "Profile does not match this user's role"),
                status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            )
        target.profile_id = str(profile_id)
        summary = f"Assigned profile {prof.get('name')} to {target.name}"
    else:
        target.profile_id = None
        summary = f"Removed permission profile from {target.name}"

    target.save(update_fields=["profile_id", "updated_at"])
    after = serialize_user(target)
    record_audit(
        actor=actor,
        action="update",
        entity_type="user",
        entity_id=target.id,
        summary=summary,
        before=before,
        after=after,
    )
    return after


def _shop_blockers(customer_id: int) -> list[str]:
    from apps.customers.services import customer_balance_map
    from apps.orders.models import Order, OrderStatus

    reasons: list[str] = []
    bal = customer_balance_map([customer_id]).get(customer_id, 0)
    if bal > 0:
        reasons.append(f"عليه دين ({bal:,} د.ع)")
    elif bal < 0:
        reasons.append(f"له رصيد ({abs(bal):,} د.ع)")

    open_orders = Order.objects.filter(
        customer_id=customer_id,
    ).exclude(status__in=[OrderStatus.CONFIRMED, OrderStatus.CANCELLED, OrderStatus.EXPIRED]).count()
    if open_orders:
        reasons.append(f"له {open_orders} طلب مفتوح")
    return reasons


@transaction.atomic
def delete_user_by_manager(*, actor: User, user_id: int) -> None:
    from apps.deliveries.models import Delivery, DeliveryStatus
    from apps.orders.models import Order, OrderStatus

    target = User.objects.select_for_update().filter(id=user_id, deleted=False).first()
    if not target:
        raise ApiException(code=ErrorCode.NOT_FOUND, status_code=http_status.HTTP_404_NOT_FOUND)
    if target.id == actor.id:
        raise ApiException(
            code=ErrorCode.BAD_STATE,
            message=("لا تستطيع حذف حسابك", "You can't delete your own account"),
            status_code=http_status.HTTP_409_CONFLICT,
        )

    why: list[str] = []
    if target.role == Role.MANAGER:
        other_managers = User.objects.filter(role=Role.MANAGER, active=True, deleted=False).exclude(id=target.id).exists()
        if not other_managers:
            why.append("هو المدير الوحيد")

    if target.role == Role.REP:
        shops = Customer.objects.filter(rep=target, active=True).count()
        if shops:
            why.append(f"له محلات ({shops}): انقلها إلى مندوب آخر")
        open_orders = Order.objects.filter(rep=target).exclude(
            status__in=[OrderStatus.CONFIRMED, OrderStatus.CANCELLED, OrderStatus.EXPIRED]
        ).count()
        if open_orders:
            why.append(f"له طلبات لم تنتهِ ({open_orders})")
        trips = Delivery.objects.filter(rep=target).exclude(
            status=DeliveryStatus.DELIVERED
        ).exclude(order__status=OrderStatus.CANCELLED).count()
        if trips:
            why.append(f"معه شحنات لم تُسلَّم ({trips})")

    if why:
        raise ApiException(
            code=ErrorCode.BAD_STATE,
            message=(f"لا يمكن حذف {target.name}: {'؛ '.join(why)}.", f"{target.name} can't be deleted yet: work is still open."),
            status_code=http_status.HTTP_409_CONFLICT,
        )

    name = target.name
    target.deleted = True
    target.active = False
    target.phone = f"del:{target.id}"[:20]
    target.profile_id = None
    target.token_version += 1
    target.save(update_fields=["deleted", "active", "phone", "profile_id", "token_version", "updated_at"])
    UserSession.objects.filter(user=target, revoked=False).update(revoked=True, revoked_at=timezone.now())
    record_audit(
        actor=actor,
        action="cancel",
        entity_type="user",
        entity_id=target.id,
        summary=f"Deleted the account of {name}",
    )


@transaction.atomic
def remove_user_photo(*, actor: User, user_id: int) -> dict[str, Any]:
    target = User.objects.select_for_update().filter(id=user_id, deleted=False).first()
    if not target:
        raise ApiException(code=ErrorCode.NOT_FOUND, status_code=http_status.HTTP_404_NOT_FOUND)
    if target.photo_path:
        get_storage().delete("product-images", target.photo_path)
        target.photo_path = ""
        target.save(update_fields=["photo_path", "updated_at"])
    record_audit(
        actor=actor,
        action="update",
        entity_type="user",
        entity_id=target.id,
        summary=f"Removed the picture of {target.name}",
    )
    return serialize_user(target)


@transaction.atomic
def set_my_photo(*, actor: User, photo: str | None, request: Any = None) -> dict[str, Any]:
    user = User.objects.select_for_update().get(id=actor.id)
    if not photo:
        if user.photo_path:
            get_storage().delete("product-images", user.photo_path)
        user.photo_path = ""
        user.save(update_fields=["photo_path", "updated_at"])
        record_audit(actor=user, action="update", entity_type="user", entity_id=user.id, summary=f"{user.name} removed their photo")
        return serialize_user(user, request=request)

    if len(str(photo)) > 400_000:
        raise ApiException(
            code=ErrorCode.VALIDATION,
            message=("الصورة كبيرة جداً", "The picture is too big"),
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
        )
    raw_bytes, ext, mime_type = decode_image_data_url(str(photo), max_bytes=300 * 1024, max_dim=512)
    key = get_storage().put_bytes("product-images", f"user-photos/{user.id}.{ext}", raw_bytes, mime_type)
    user.photo_path = key
    user.save(update_fields=["photo_path", "updated_at"])
    record_audit(actor=user, action="update", entity_type="user", entity_id=user.id, summary=f"{user.name} updated their photo")
    return serialize_user(user, request=request)


@transaction.atomic
def change_my_password(*, actor: User, current_password: str, new_password: str) -> dict[str, bool]:
    user = User.objects.select_for_update().get(id=actor.id)
    if not user.check_password(current_password or ""):
        msg = ("كلمة المرور الحالية غير صحيحة", "Wrong current password")
        raise ApiException(
            code=ErrorCode.VALIDATION,
            message=msg,
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            errors={"current": msg},
        )
    if len(new_password or "") < 6:
        msg = ("كلمة المرور الجديدة يجب أن تكون 6 أحرف على الأقل", "New password must be at least 6 characters")
        raise ApiException(
            code=ErrorCode.VALIDATION,
            message=msg,
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            errors={"next": msg},
        )
    user.set_password(new_password)
    user.weak_password = is_weak_password(new_password, user.phone)
    user.token_version += 1
    user.save(update_fields=["password", "weak_password", "token_version", "updated_at"])
    record_audit(actor=user, action="update", entity_type="user", entity_id=user.id, summary=f"{user.name} changed password")
    return {"ok": True}


@transaction.atomic
def remove_my_shop(*, actor: User, customer_id: int, password: str, request: Any = None) -> dict[str, Any]:
    if actor.role != Role.SHOP:
        raise ApiException(code=ErrorCode.FORBIDDEN, status_code=http_status.HTTP_403_FORBIDDEN)
    user = User.objects.select_for_update().get(id=actor.id)
    if not user.check_password(password or ""):
        msg = ("كلمة المرور غير صحيحة", "Wrong password")
        raise ApiException(
            code=ErrorCode.VALIDATION,
            message=msg,
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            errors={"password": msg},
        )

    memberships = list(ShopMembership.objects.filter(user=user).order_by("id"))
    if not any(m.customer_id == customer_id for m in memberships):
        raise ApiException(
            code=ErrorCode.FORBIDDEN,
            message=("هذا المحل ليس لك", "This shop isn't yours"),
            status_code=http_status.HTTP_403_FORBIDDEN,
        )
    if len(memberships) <= 1:
        raise ApiException(
            code=ErrorCode.BAD_STATE,
            message=("هذا محلك الوحيد: لإزالته احذف الحساب كله", "This is your only shop: delete the whole account instead"),
            status_code=http_status.HTTP_409_CONFLICT,
        )

    why = _shop_blockers(customer_id)
    if why:
        raise ApiException(
            code=ErrorCode.BAD_STATE,
            message=(f"لا يمكن إزالته الآن: {'؛ '.join(why)}.", "It can't be removed now: money or orders are still open."),
            status_code=http_status.HTTP_409_CONFLICT,
        )

    cust = Customer.objects.select_for_update().filter(id=customer_id).first()
    if not cust:
        raise ApiException(code=ErrorCode.NOT_FOUND, status_code=http_status.HTTP_404_NOT_FOUND)
    cust.active = False
    cust.save(update_fields=["active", "updated_at"])

    ShopMembership.objects.filter(user=user, customer_id=customer_id).delete()
    remaining_ms = list(ShopMembership.objects.filter(user=user).order_by("id"))
    next_cid = remaining_ms[0].customer_id if remaining_ms else None
    if remaining_ms and not any(m.is_active_shop for m in remaining_ms):
        ShopMembership.objects.filter(id=remaining_ms[0].id).update(is_active_shop=True)
    if next_cid is not None:
        UserSession.objects.filter(user=user, active_customer_id=customer_id).update(active_customer_id=next_cid)

    record_audit(
        actor=user,
        action="cancel",
        entity_type="customer",
        entity_id=cust.id,
        summary=f"{user.name} removed shop {cust.name}",
    )
    notify_role(
        role=Role.MANAGER,
        kind="new_shop",
        title_ar=f"{user.name} أزال محل {cust.name} من حسابه",
        title_en=f"{user.name} removed the shop {cust.name}",
        body_ar="المحل موقوف الآن، وسجلاته وفواتيره باقية",
        body_en="The shop is off now; its records and invoices stay",
        ref_type="customer",
        ref_id=cust.id,
    )
    return serialize_user(
        user,
        active_customer_id=next_cid,
        customer_ids=[m.customer_id for m in remaining_ms],
        request=request,
    )


@transaction.atomic
def delete_my_account(*, actor: User, password: str) -> dict[str, bool]:
    if actor.role != Role.SHOP:
        raise ApiException(code=ErrorCode.FORBIDDEN, status_code=http_status.HTTP_403_FORBIDDEN)
    user = User.objects.select_for_update().get(id=actor.id)
    if not user.check_password(password or ""):
        msg = ("كلمة المرور غير صحيحة", "Wrong password")
        raise ApiException(
            code=ErrorCode.VALIDATION,
            message=msg,
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            errors={"password": msg},
        )

    shop_ids = list(ShopMembership.objects.filter(user=user).values_list("customer_id", flat=True))
    why: list[str] = []
    for cid in shop_ids:
        why.extend(_shop_blockers(cid))
    if why:
        raise ApiException(
            code=ErrorCode.BAD_STATE,
            message=(f"لا يمكن حذف الحساب الآن: {'؛ '.join(why)}.", "The account can't be deleted now: money or orders are still open."),
            status_code=http_status.HTTP_409_CONFLICT,
        )

    customers = list(Customer.objects.filter(id__in=shop_ids))
    Customer.objects.filter(id__in=shop_ids).update(active=False)

    name = user.name
    user.deleted = True
    user.active = False
    user.phone = f"del:{user.id}"[:20]
    user.token_version += 1
    user.save(update_fields=["deleted", "active", "phone", "token_version", "updated_at"])
    UserSession.objects.filter(user=user, revoked=False).update(revoked=True, revoked_at=timezone.now())

    record_audit(
        actor=user,
        action="cancel",
        entity_type="user",
        entity_id=user.id,
        summary=f"{name} deleted their account",
    )
    notify_role(
        role=Role.MANAGER,
        kind="new_shop",
        title_ar=f"{name} حذف حسابه",
        title_en=f"{name} deleted his account",
        body_ar=(
            f"محلاته موقوفة الآن: {', '.join(c.name for c in customers)}. سجلاتها باقية"
            if customers
            else "سجلاته باقية"
        ),
        body_en="His shops are off now; their records stay",
        ref_type="customer",
        ref_id=customers[0].id if customers else "",
    )
    return {"ok": True}

