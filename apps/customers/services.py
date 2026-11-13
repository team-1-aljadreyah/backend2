"""Customer, ShopSwitcher, SpecialPrice, and Balance computation services."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from django.apps import apps
from django.db import transaction
from django.db.models import Max, Q, Sum
from rest_framework import status as http_status

from apps.accounts.models import PriceTier, Role, User, UserSession
from apps.accounts.permissions import get_active_customer_id, sees_debt
from apps.accounts.profiles import (
    batch_custom_values,
    custom_values_for_object,
    validate_and_apply_custom_on_create,
)
from apps.accounts.serializers import serialize_user
from apps.audit.services import record_audit
from apps.catalog.models import Item
from apps.common.exceptions import ApiException, ErrorCode, localize
from apps.common.utils import normalize_phone
from apps.customers.models import Customer, CustomerStatus, ShopMembership, SpecialPrice
from apps.notifications.services import notify_role, notify_user


def customer_balance_map(customer_ids: list[int] | None = None) -> dict[int, int]:
    """
    Compute exact net IQD customer balance:
    sum(total - paid - returned - credited across invoices)
    - sum(unallocated credit on non-reversed payments)
    + sum(refunds.amount)
    Negative balance means credit in the customer's favor (overpayment, return, or credit note).
    """
    balances: dict[int, int] = {}
    if apps.is_installed("apps.invoices"):
        from apps.invoices.models import Invoice

        inv_qs = Invoice.objects.all()
        if customer_ids is not None:
            inv_qs = inv_qs.filter(customer_id__in=customer_ids)
        for inv in inv_qs.only("customer_id", "total", "paid", "returned", "credited"):
            rem = inv.total - inv.paid - inv.returned - inv.credited
            balances[inv.customer_id] = balances.get(inv.customer_id, 0) + rem

    if apps.is_installed("apps.payments"):
        from apps.payments.models import Payment, Refund

        pay_qs = Payment.objects.filter(reversed=False, reversal_of__isnull=True, unallocated_amount__gt=0)
        if customer_ids is not None:
            pay_qs = pay_qs.filter(customer_id__in=customer_ids)
        for row in pay_qs.values("customer_id").annotate(u=Sum("unallocated_amount")):
            balances[row["customer_id"]] = balances.get(row["customer_id"], 0) - int(row["u"] or 0)

        ref_qs = Refund.objects.all()
        if customer_ids is not None:
            ref_qs = ref_qs.filter(customer_id__in=customer_ids)
        for row in ref_qs.values("customer_id").annotate(r=Sum("amount")):
            balances[row["customer_id"]] = balances.get(row["customer_id"], 0) + int(row["r"] or 0)

    return balances


def last_order_at_map(customer_ids: list[int] | None = None) -> dict[int, str | None]:
    if not apps.is_installed("apps.orders"):
        return {}
    from apps.orders.models import Order

    qs = Order.objects.all()
    if customer_ids is not None:
        qs = qs.filter(customer_id__in=customer_ids)
    rows = qs.values("customer_id").annotate(latest=Max("created_at"))
    return {r["customer_id"]: (r["latest"].isoformat() if r["latest"] else None) for r in rows}


def serialize_customer(
    cust: Customer,
    *,
    actor: User,
    balance_map: dict[int, int] | None = None,
    last_order_map: dict[int, str | None] | None = None,
    custom_values: dict[str, Any] | None = None,
) -> dict[str, Any]:
    can_debt = sees_debt(actor)
    lmap = last_order_map if last_order_map is not None else last_order_at_map([cust.id])
    composed_address = cust.address_text or " - ".join(
        p for p in [cust.city, cust.area, cust.street, cust.landmark] if p
    )
    out: dict[str, Any] = {
        "id": cust.id,
        "name": cust.name,
        "owner_name": cust.owner_name,
        "phone": cust.phone,
        "city": cust.city,
        "area": cust.area,
        "street": cust.street,
        "landmark": cust.landmark,
        "address": composed_address,
        "tier_id": cust.tier_id,
        "rep_id": cust.rep_id or 0,
        "active": bool(cust.active),
        "pending": cust.status == CustomerStatus.PENDING,
        "status": cust.status,
        "reject_reason": cust.reject_reason,
        "visit_days": list(cust.visit_days or []),
        "last_order_at": lmap.get(cust.id),
    }
    if can_debt:
        bmap = balance_map if balance_map is not None else customer_balance_map([cust.id])
        out["debt_limit"] = cust.debt_limit
        out["balance"] = bmap.get(cust.id, 0)
    if cust.lat is not None and cust.lng is not None:
        out["lat"] = float(cust.lat)
        out["lng"] = float(cust.lng)
    if isinstance(cust.address_info, dict) and cust.address_info:
        out["address_info"] = cust.address_info
    if custom_values:
        out["custom"] = custom_values
    return out


def can_see_customer(actor: User, customer: Customer) -> bool:
    if actor.role in (Role.MANAGER, Role.ACCOUNTANT, Role.WAREHOUSE, Role.STAFF):
        return True
    if actor.role == Role.REP:
        if customer.rep_id == actor.id:
            return True
        if apps.is_installed("apps.deliveries"):
            from apps.deliveries.models import Delivery

            if Delivery.objects.filter(rep_id=actor.id, order__customer_id=customer.id).exists():
                return True
        return False
    if actor.role == Role.SHOP:
        cid = get_active_customer_id(actor)
        return cid is not None and cid == customer.id
    return False


def list_customers_for_actor(*, actor: User) -> list[dict[str, Any]]:
    qs = Customer.objects.filter(walk_in=False).order_by("id")
    if actor.role == Role.REP:
        q = Q(rep_id=actor.id)
        if apps.is_installed("apps.deliveries"):
            from apps.deliveries.models import Delivery

            del_cids = list(Delivery.objects.filter(rep_id=actor.id).values_list("order__customer_id", flat=True))
            if del_cids:
                q |= Q(id__in=del_cids)
        qs = qs.filter(q).distinct()
    elif actor.role == Role.SHOP:
        cid = get_active_customer_id(actor)
        qs = qs.filter(id=cid) if cid else qs.none()

    customers = list(qs)
    cids = [c.id for c in customers]
    bmap = customer_balance_map(cids) if sees_debt(actor) else {}
    lmap = last_order_at_map(cids)
    cmap = batch_custom_values("customer", cids, actor)
    return [
        serialize_customer(c, actor=actor, balance_map=bmap, last_order_map=lmap, custom_values=cmap.get(c.id))
        for c in customers
    ]


def get_customer_for_actor(*, actor: User, customer_id: int) -> dict[str, Any]:
    cust = Customer.objects.filter(id=customer_id).first()
    if not cust:
        raise ApiException(code=ErrorCode.NOT_FOUND, status_code=http_status.HTTP_404_NOT_FOUND)
    if not can_see_customer(actor, cust):
        raise ApiException(code=ErrorCode.FORBIDDEN, status_code=http_status.HTTP_403_FORBIDDEN)
    return serialize_customer(
        cust,
        actor=actor,
        custom_values=custom_values_for_object("customer", cust.id, actor),
    )


def clean_shop_address(raw: Any) -> dict[str, Any]:
    """Validate and normalize a structured ShopAddress object."""
    if not isinstance(raw, dict):
        msg = ("معلومات العنوان غير صالحة", "Invalid address_info payload")
        raise ApiException(code=ErrorCode.VALIDATION, message=msg, errors={"address_info": msg})

    def _txt(val: Any) -> str:
        return str(val or "").strip()[:160]

    errors: dict[str, tuple[str, str]] = {}
    label = _txt(raw.get("label"))
    details = _txt(raw.get("details"))
    if not label:
        errors["address_info.label"] = ("اسم العنوان مطلوب", "Address label is required")
    if not details:
        errors["address_info.details"] = ("تفاصيل العنوان مطلوبة", "Address details are required")

    try:
        phone = normalize_phone(raw.get("phone"))
    except ApiException as exc:
        errors["address_info.phone"] = (
            localize(exc.message, "ar") or "رقم هاتف غير صالح",
            localize(exc.message, "en") or "Invalid phone number",
        )
        phone = ""

    lat_val = raw.get("lat")
    lng_val = raw.get("lng")
    pin_lat: float | None = None
    pin_lng: float | None = None
    if lat_val not in (None, "") and lng_val not in (None, ""):
        try:
            f_lat = float(lat_val)
            f_lng = float(lng_val)
            if 29 < f_lat < 38 and 38 < f_lng < 49:
                pin_lat = round(f_lat, 6)
                pin_lng = round(f_lng, 6)
        except (TypeError, ValueError):
            pass

    if errors:
        raise ApiException(code=ErrorCode.VALIDATION, message=next(iter(errors.values())), errors=errors)

    info: dict[str, Any] = {
        "label": label,
        "details": details,
        "phone": phone,
    }
    for opt_key in ("street", "building", "floor", "apartment", "landmark"):
        val = _txt(raw.get(opt_key))
        if val:
            info[opt_key] = val
    if pin_lat is not None and pin_lng is not None:
        info["lat"] = pin_lat
        info["lng"] = pin_lng
    return info


def format_address_text(info: dict[str, Any] | None) -> str:
    if not info:
        return ""
    parts = [
        info.get("label", ""),
        info.get("street", ""),
        info.get("details", "") if info.get("details") != info.get("label") else "",
        f"قرب {info['landmark']}" if info.get("landmark") else "",
    ]
    return " - ".join(p for p in parts if p)[:255]


def _validate_address_and_core(data: dict[str, Any], *, partial: bool = False) -> dict[str, Any]:
    cleaned: dict[str, Any] = {}
    if "name" in data or not partial:
        val = str(data.get("name") or "").strip()
        if not val:
            msg = ("اسم المحل مطلوب", "Shop name is required")
            raise ApiException(code=ErrorCode.VALIDATION, message=msg, errors={"name": msg})
        cleaned["name"] = val

    if "area" in data or not partial:
        val = str(data.get("area") or "").strip()
        if not val:
            msg = ("المنطقة مطلوبة", "Area is required")
            raise ApiException(code=ErrorCode.VALIDATION, message=msg, errors={"area": msg})
        cleaned["area"] = val

    if "address_info" in data:
        raw_ai = data.get("address_info")
        if raw_ai is None:
            cleaned["address_info"] = None
        else:
            ai = clean_shop_address(raw_ai)
            cleaned["address_info"] = ai
            cleaned["address_text"] = format_address_text(ai)
            if "lat" in ai and "lng" in ai:
                cleaned["lat"] = Decimal(str(ai["lat"]))
                cleaned["lng"] = Decimal(str(ai["lng"]))

    raw_addr = str(data.get("address") or "").strip()
    if "address" in data and raw_addr and not cleaned.get("address_text"):
        cleaned["address_text"] = raw_addr[:255]

    for key, ar_lbl, en_lbl in [
        ("city", "المدينة", "City"),
        ("street", "الشارع", "Street"),
        ("landmark", "نقطة دالة", "Landmark"),
    ]:
        if key in data:
            val = str(data.get(key) or "").strip()
            if not val and not partial and not cleaned.get("address_info") and not raw_addr:
                msg = (f"{ar_lbl} مطلوب", f"{en_lbl} is required")
                raise ApiException(code=ErrorCode.VALIDATION, message=msg, errors={key: msg})
            cleaned[key] = val
        elif not partial:
            ai_dict = cleaned.get("address_info") or {}
            if key == "city":
                cleaned["city"] = ""
            elif key == "street":
                cleaned["street"] = str(ai_dict.get("street") or ai_dict.get("details") or raw_addr or "").strip()
            elif key == "landmark":
                cleaned["landmark"] = str(ai_dict.get("landmark") or ai_dict.get("label") or raw_addr or "").strip()

    if not partial and not cleaned.get("address_text"):
        composed = " - ".join(
            p for p in [cleaned.get("city", ""), cleaned.get("area", ""), cleaned.get("street", ""), cleaned.get("landmark", "")] if p
        )
        cleaned["address_text"] = composed[:255]

    if "owner_name" in data:
        cleaned["owner_name"] = str(data.get("owner_name") or "").strip()
    if "phone" in data or not partial:
        cleaned["phone"] = normalize_phone(data.get("phone"))
    for coord in ("lat", "lng"):
        if coord in data:
            val = data[coord]
            if val in (None, ""):
                cleaned[coord] = None
            else:
                try:
                    cleaned[coord] = Decimal(str(val))
                except InvalidOperation as exc:
                    raise ApiException(code=ErrorCode.VALIDATION, message=("إحداثيات غير صالحة", "Invalid coordinates")) from exc
    return cleaned


@transaction.atomic
def create_customer(*, actor: User, data: dict[str, Any]) -> dict[str, Any]:
    cleaned = _validate_address_and_core(data, partial=False)
    phone = cleaned["phone"]
    if Customer.objects.filter(phone=phone).exists():
        msg = ("رقم هاتف الزبون مسجل مسبقاً", "Customer phone number already exists")
        raise ApiException(code=ErrorCode.VALIDATION, message=msg, errors={"phone": msg})

    tier_id = data.get("tier_id")
    tier = PriceTier.objects.filter(id=tier_id).first() if tier_id else PriceTier.objects.order_by("sort_order", "id").first()
    if not tier:
        raise ApiException(code=ErrorCode.VALIDATION, message=("فئة السعر غير موجودة", "Price tier not found"))

    if actor.role == Role.REP:
        rep_id = actor.id
        debt_limit = 0
    else:
        rep_id = data.get("rep_id") or None
        try:
            debt_limit = int(data.get("debt_limit") or 0)
            if debt_limit < 0:
                raise ValueError
        except (TypeError, ValueError) as exc:
            raise ApiException(code=ErrorCode.VALIDATION, message=("سقف الدين غير صحيح", "Invalid debt limit")) from exc

    rep = User.objects.filter(id=rep_id, role=Role.REP, active=True).first() if rep_id else None

    cust = Customer.objects.create(
        name=cleaned["name"],
        owner_name=cleaned.get("owner_name") or cleaned["name"],
        phone=phone,
        city=cleaned.get("city", ""),
        area=cleaned["area"],
        street=cleaned.get("street", ""),
        landmark=cleaned.get("landmark", ""),
        address_text=cleaned.get("address_text", ""),
        address_info=cleaned.get("address_info"),
        lat=cleaned.get("lat"),
        lng=cleaned.get("lng"),
        tier=tier,
        rep=rep,
        debt_limit=debt_limit,
        active=bool(data.get("active", True)),
        status=CustomerStatus.ACTIVE,
        approved_by=actor,
    )
    custom_vals = validate_and_apply_custom_on_create("customer", cust.id, actor, data.get("custom"))
    record_audit(
        actor=actor,
        action="customer.create",
        entity_type="customer",
        entity_id=cust.id,
        summary=f"Created customer {cust.name}",
        after={"name": cust.name, "phone": cust.phone, "debt_limit": cust.debt_limit},
    )
    return serialize_customer(cust, actor=actor, custom_values=custom_vals)


@transaction.atomic
def update_customer(*, actor: User, customer_id: int, data: dict[str, Any]) -> dict[str, Any]:
    cust = Customer.objects.select_for_update().filter(id=customer_id).first()
    if not cust:
        raise ApiException(code=ErrorCode.NOT_FOUND, status_code=http_status.HTTP_404_NOT_FOUND)

    before = {"status": cust.status, "active": cust.active, "tier_id": cust.tier_id, "debt_limit": cust.debt_limit, "rep_id": cust.rep_id}
    was_pending = cust.status == CustomerStatus.PENDING
    cleaned = _validate_address_and_core(data, partial=True)

    if "phone" in cleaned and cleaned["phone"] != cust.phone:
        if Customer.objects.filter(phone=cleaned["phone"]).exclude(id=cust.id).exists():
            msg = ("رقم الهاتف مستخدم مسبقاً", "Phone number already in use")
            raise ApiException(code=ErrorCode.VALIDATION, message=msg, errors={"phone": msg})

    for k, v in cleaned.items():
        setattr(cust, k, v)

    if "tier_id" in data and data["tier_id"]:
        tier = PriceTier.objects.filter(id=data["tier_id"]).first()
        if not tier:
            raise ApiException(code=ErrorCode.VALIDATION, message=("فئة السعر غير موجودة", "Price tier not found"))
        cust.tier = tier

    if "rep_id" in data:
        rid = data["rep_id"]
        if rid in (None, "", 0):
            cust.rep = None
        else:
            rep = User.objects.filter(id=rid, role=Role.REP).first()
            if not rep:
                raise ApiException(code=ErrorCode.VALIDATION, message=("المندوب غير موجود", "Rep user not found"))
            cust.rep = rep

    if "debt_limit" in data:
        try:
            dl = int(data["debt_limit"])
            if dl < 0:
                raise ValueError
        except (TypeError, ValueError) as exc:
            raise ApiException(code=ErrorCode.VALIDATION, message=("سقف الدين غير صحيح", "Invalid debt limit")) from exc
        cust.debt_limit = dl

    approving = False
    if "pending" in data and data["pending"] is False and was_pending:
        approving = True
    if "active" in data:
        cust.active = bool(data["active"])
        if cust.active and was_pending:
            approving = True

    if approving:
        cust.status = CustomerStatus.ACTIVE
        cust.active = True
        cust.approved_by = actor
        cust.reject_reason = ""

    cust.save()

    if approving:
        for ms in cust.memberships.select_related("user"):
            notify_user(
                user_id=ms.user_id,
                kind="shop_approved",
                title_ar=f"تم تفعيل حساب محلك {cust.name}",
                title_en=f"Your shop {cust.name} has been approved",
                ref_type="customer",
                ref_id=cust.id,
            )

    record_audit(
        actor=actor,
        action="customer.approve" if approving else "customer.update",
        entity_type="customer",
        entity_id=cust.id,
        summary=f"{'Approved' if approving else 'Updated'} customer {cust.name}",
        before=before,
        after={"status": cust.status, "active": cust.active, "tier_id": cust.tier_id, "debt_limit": cust.debt_limit},
    )
    return serialize_customer(cust, actor=actor)


@transaction.atomic
def reject_customer(*, actor: User, customer_id: int, reason: str) -> dict[str, Any]:
    cust = Customer.objects.select_for_update().filter(id=customer_id).first()
    if not cust:
        raise ApiException(code=ErrorCode.NOT_FOUND, status_code=http_status.HTTP_404_NOT_FOUND)
    reason_clean = (reason or "").strip()
    if not reason_clean:
        msg = ("سبب الرفض مطلوب", "Rejection reason is required")
        raise ApiException(code=ErrorCode.VALIDATION, message=msg, errors={"reason": msg})

    cust.status = CustomerStatus.REJECTED
    cust.active = False
    cust.reject_reason = reason_clean
    cust.save(update_fields=["status", "active", "reject_reason"])

    for ms in cust.memberships.select_related("user"):
        notify_user(
            user_id=ms.user_id,
            kind="shop_rejected",
            title_ar=f"تم رفض طلب تسجيل {cust.name}",
            title_en=f"Registration for {cust.name} was rejected",
            body_ar=reason_clean,
            body_en=reason_clean,
            ref_type="customer",
            ref_id=cust.id,
        )

    record_audit(
        actor=actor,
        action="customer.reject",
        entity_type="customer",
        entity_id=cust.id,
        summary=f"Rejected shop {cust.name}: {reason_clean}",
    )
    return serialize_customer(cust, actor=actor)


def list_special_prices(*, actor: User, customer_id: int) -> list[dict[str, Any]]:
    cust = Customer.objects.filter(id=customer_id).first()
    if not cust:
        raise ApiException(code=ErrorCode.NOT_FOUND, status_code=http_status.HTTP_404_NOT_FOUND)
    if not can_see_customer(actor, cust):
        raise ApiException(code=ErrorCode.FORBIDDEN, status_code=http_status.HTTP_403_FORBIDDEN)
    return [
        {
            "customer_id": sp.customer_id,
            "item_id": sp.item_id,
            "carton_price": sp.carton_price,
            "piece_price": sp.piece_price,
            "by": {"id": sp.updated_by_id or 0, "name": sp.updated_by.name if sp.updated_by else ""},
            "at": sp.updated_at.isoformat(),
        }
        for sp in SpecialPrice.objects.filter(customer=cust).select_related("updated_by").order_by("item_id")
    ]


@transaction.atomic
def set_special_price(*, actor: User, customer_id: int, data: dict[str, Any]) -> list[dict[str, Any]]:
    cust = Customer.objects.filter(id=customer_id).first()
    if not cust:
        raise ApiException(code=ErrorCode.NOT_FOUND, status_code=http_status.HTTP_404_NOT_FOUND)
    item = Item.objects.filter(id=data.get("item_id")).first()
    if not item:
        raise ApiException(code=ErrorCode.NOT_FOUND, status_code=http_status.HTTP_404_NOT_FOUND)

    prices = data.get("prices")
    if prices is None:
        SpecialPrice.objects.filter(customer=cust, item=item).delete()
    else:
        try:
            cp = int(prices["carton_price"])
            pp = int(prices["piece_price"])
            if cp < 0 or pp < 0:
                raise ValueError
        except (KeyError, TypeError, ValueError) as exc:
            raise ApiException(code=ErrorCode.VALIDATION, message=("الأسعار الخاصة غير صالحة", "Invalid special price values")) from exc
        SpecialPrice.objects.update_or_create(
            customer=cust,
            item=item,
            defaults={"carton_price": cp, "piece_price": pp, "updated_by": actor},
        )
    record_audit(
        actor=actor,
        action="customer.special_price",
        entity_type="customer",
        entity_id=cust.id,
        summary=f"Updated special price for item #{item.id}",
    )
    return list_special_prices(actor=actor, customer_id=cust.id)


def list_my_shops(*, actor: User) -> list[dict[str, Any]]:
    memberships = list(ShopMembership.objects.filter(user=actor).select_related("customer").order_by("id"))
    cids = [m.customer_id for m in memberships]
    bmap = customer_balance_map(cids)
    return [
        {
            "id": m.customer_id,
            "customer_id": m.customer_id,
            "name": m.customer.name,
            "area": m.customer.area,
            "city": m.customer.city,
            "pending": m.customer.status == CustomerStatus.PENDING,
            "active": bool(m.customer.active),
            "is_primary": bool(m.is_primary),
            "balance": bmap.get(m.customer_id, 0),
        }
        for m in memberships
    ]


@transaction.atomic
def switch_active_shop(
    *, actor: User, customer_id: int, session_id: str | None = None, request: Any = None
) -> dict[str, Any]:
    ms = ShopMembership.objects.select_for_update().filter(user=actor, customer_id=customer_id).first()
    if not ms:
        raise ApiException(code=ErrorCode.FORBIDDEN, status_code=http_status.HTTP_403_FORBIDDEN)
    sid = session_id or getattr(actor, "_session_id", None)
    if sid:
        updated = UserSession.objects.filter(session_id=str(sid), user=actor).update(active_customer_id=ms.customer_id)
        actor._session_active_customer_id = ms.customer_id
        if not updated:
            ShopMembership.objects.filter(user=actor, is_active_shop=True).exclude(id=ms.id).update(is_active_shop=False)
            if not ms.is_active_shop:
                ms.is_active_shop = True
                ms.save(update_fields=["is_active_shop"])
    else:
        ShopMembership.objects.filter(user=actor, is_active_shop=True).exclude(id=ms.id).update(is_active_shop=False)
        if not ms.is_active_shop:
            ms.is_active_shop = True
            ms.save(update_fields=["is_active_shop"])
    actor._cached_active_customer_id = ms.customer_id
    from apps.common.rls import apply_pg_session_context

    apply_pg_session_context(
        user_id=actor.id,
        role=actor.role,
        warehouse_id=actor.warehouse_id,
        active_customer_id=ms.customer_id,
    )
    return serialize_user(actor, active_customer_id=ms.customer_id, request=request)


@transaction.atomic
def create_additional_shop(*, actor: User, data: dict[str, Any]) -> dict[str, Any]:
    if ShopMembership.objects.filter(user=actor).count() >= 10:
        msg = ("حتى 10 محلات لكل حساب", "Up to 10 shops per account")
        raise ApiException(code=ErrorCode.VALIDATION, message=msg, errors={"shop_name": msg})

    shop_name = str(data.get("shop_name") or data.get("name") or "").strip()
    if Customer.objects.filter(name=shop_name).exists():
        msg = ("يوجد محل بهذا الاسم", "A shop with this name exists")
        raise ApiException(code=ErrorCode.VALIDATION, message=msg, errors={"shop_name": msg})

    cleaned = _validate_address_and_core(
        {**data, "name": shop_name, "phone": data.get("phone") or actor.phone},
        partial=False,
    )
    primary = ShopMembership.objects.filter(user=actor).select_related("customer").order_by("-is_primary", "id").first()
    tier = primary.customer.tier if primary else PriceTier.objects.order_by("sort_order", "id").first()
    if not tier:
        tier = PriceTier.objects.create(name="جملة", name_en="Wholesale", sort_order=1)

    assigned_rep = None
    if primary and primary.customer.rep_id:
        assigned_rep = primary.customer.rep
    else:
        assigned_rep = (
            User.objects.filter(role=Role.REP, active=True, assigned_customers__area=cleaned["area"]).first()
            or User.objects.filter(role=Role.REP, active=True).order_by("id").first()
        )

    branch = Customer.objects.create(
        name=cleaned["name"],
        owner_name=actor.name,
        phone=cleaned["phone"],
        city=cleaned.get("city", ""),
        area=cleaned["area"],
        street=cleaned.get("street", ""),
        landmark=cleaned.get("landmark", ""),
        address_text=cleaned.get("address_text", "") or f"كربلاء - {cleaned['area']}",
        address_info=cleaned.get("address_info"),
        lat=cleaned.get("lat"),
        lng=cleaned.get("lng"),
        tier=tier,
        rep=assigned_rep,
        debt_limit=0,
        active=False,
        status=CustomerStatus.PENDING,
    )
    ShopMembership.objects.create(user=actor, customer=branch, is_primary=False, is_active_shop=False)
    notify_role(
        role=Role.MANAGER,
        kind="branch_added",
        title_ar=f"إضافة فرع جديد لمحل: {branch.name}",
        title_en=f"New branch shop added: {branch.name}",
        ref_type="customer",
        ref_id=branch.id,
    )
    record_audit(
        actor=actor,
        action="shop.add_branch",
        entity_type="customer",
        entity_id=branch.id,
        summary=f"Shop user added branch {branch.name}",
    )
    return {
        "id": branch.id,
        "customer_id": branch.id,
        "name": branch.name,
        "area": branch.area,
        "city": branch.city,
        "pending": True,
        "active": False,
        "is_primary": False,
        "balance": 0,
    }


def serialize_shop_list(sl: Any) -> dict[str, Any]:
    return {
        "id": sl.id,
        "customer_id": sl.customer_id,
        "name": sl.name,
        "lines": sl.lines,
        "updated_at": sl.updated_at.isoformat(),
    }


def list_shop_lists(*, actor: User) -> list[dict[str, Any]]:
    if actor.role != Role.SHOP:
        raise ApiException(
            code=ErrorCode.FORBIDDEN,
            status_code=http_status.HTTP_403_FORBIDDEN,
            message=("أصحاب المتاجر فقط", "Shop owners only"),
        )
    cid = get_active_customer_id(actor)
    if not cid:
        return []
    from apps.customers.models import ShopList

    qs = ShopList.objects.filter(customer_id=cid).order_by("-updated_at", "-id")
    return [serialize_shop_list(sl) for sl in qs]


@transaction.atomic
def save_shop_list(*, actor: User, list_id: int | None, data: dict[str, Any]) -> dict[str, Any]:
    if actor.role != Role.SHOP:
        raise ApiException(
            code=ErrorCode.FORBIDDEN,
            status_code=http_status.HTTP_403_FORBIDDEN,
            message=("أصحاب المتاجر فقط", "Shop owners only"),
        )
    cid = get_active_customer_id(actor)
    if not cid:
        raise ApiException(
            code=ErrorCode.FORBIDDEN,
            status_code=http_status.HTTP_403_FORBIDDEN,
            message=("اختر متجراً أولاً", "Choose a shop first"),
        )

    from apps.catalog.models import Item
    from apps.customers.models import ShopList

    name = str(data.get("name") or "").strip()
    errors: dict[str, tuple[str, str]] = {}
    if not name or len(name) > 40:
        errors["name"] = ("اسم القائمة مطلوب (40 حرفاً على الأكثر)", "The list needs a name (40 letters at most)")

    raw_lines = data.get("lines") or []
    if not isinstance(raw_lines, list):
        errors["lines"] = ("صيغة المواد غير صحيحة", "Invalid lines format")
        raw_lines = []

    clean_lines: list[dict[str, Any]] = []
    item_ids = [int(line.get("item_id", 0)) for line in raw_lines if isinstance(line, dict) and line.get("item_id")]
    existing_items = {it.id: it for it in Item.objects.filter(id__in=item_ids)}

    for i, line in enumerate(raw_lines):
        if not isinstance(line, dict):
            continue
        item_id = int(line.get("item_id", 0))
        unit = str(line.get("unit") or "")
        qty = line.get("qty")
        it = existing_items.get(item_id)
        if not it or it.deleted:
            errors[f"lines.{i}"] = ("مادة غير موجودة", "Unknown item")
        if unit not in ("carton", "piece"):
            errors[f"lines.{i}"] = ("كمية غير صحيحة", "Wrong quantity")
        try:
            qty_int = int(qty)
            if qty_int <= 0:
                continue
        except (TypeError, ValueError):
            errors[f"lines.{i}"] = ("كمية غير صحيحة", "Wrong quantity")
            continue
        clean_lines.append({"item_id": item_id, "unit": unit, "qty": qty_int})

    if len(clean_lines) > 80:
        errors["lines"] = ("80 مادة على الأكثر", "80 items at most")

    if errors:
        raise ApiException(code=ErrorCode.VALIDATION, message=next(iter(errors.values())), errors=errors)

    if list_id:
        sl = ShopList.objects.select_for_update().filter(id=list_id, customer_id=cid).first()
        if not sl:
            raise ApiException(code=ErrorCode.NOT_FOUND, status_code=http_status.HTTP_404_NOT_FOUND)
        sl.name = name
        sl.lines = clean_lines
        sl.save()
        return serialize_shop_list(sl)
    else:
        if ShopList.objects.filter(customer_id=cid).count() >= 30:
            raise ApiException(
                code=ErrorCode.BAD_STATE,
                status_code=http_status.HTTP_409_CONFLICT,
                message=("30 قائمة على الأكثر", "30 lists at most"),
            )
        sl = ShopList.objects.create(customer_id=cid, name=name, lines=clean_lines)
        return serialize_shop_list(sl)


@transaction.atomic
def delete_shop_list(*, actor: User, list_id: int) -> None:
    if actor.role != Role.SHOP:
        raise ApiException(
            code=ErrorCode.FORBIDDEN,
            status_code=http_status.HTTP_403_FORBIDDEN,
            message=("أصحاب المتاجر فقط", "Shop owners only"),
        )
    cid = get_active_customer_id(actor)
    if not cid:
        raise ApiException(code=ErrorCode.NOT_FOUND, status_code=http_status.HTTP_404_NOT_FOUND)
    from apps.customers.models import ShopList

    sl = ShopList.objects.filter(id=list_id, customer_id=cid).first()
    if not sl:
        raise ApiException(code=ErrorCode.NOT_FOUND, status_code=http_status.HTTP_404_NOT_FOUND)
    sl.delete()

