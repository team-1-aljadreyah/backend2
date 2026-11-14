"""Serializers for accounts, lookups, settings, and price tiers."""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from django.utils import timezone
from rest_framework import serializers

from apps.accounts.models import PriceTier, Role, SystemSetting, User
from apps.accounts.permissions import get_active_customer_id
from apps.warehouses.models import Warehouse
from integrations.storage import get_storage


def serialize_user(
    user: User,
    *,
    active_customer_id: int | None = None,
    customer_ids: list[int] | None = None,
    request: Any = None,
) -> dict[str, Any]:
    cid = active_customer_id if active_customer_id is not None else get_active_customer_id(user)
    if customer_ids is None:
        if user.role == Role.SHOP:
            from apps.customers.models import ShopMembership

            customer_ids = list(
                ShopMembership.objects.filter(user=user).order_by("id").values_list("customer_id", flat=True)
            )
        else:
            customer_ids = []
    out: dict[str, Any] = {
        "id": user.id,
        "name": user.name,
        "phone": "" if user.deleted else user.phone,
        "role": user.role,
        "active": bool(user.active and not user.deleted),
        "deleted": bool(user.deleted),
        "warehouse_id": user.warehouse_id,
        "customer_id": cid,
        "customer_ids": customer_ids,
        "rep_kinds": list(user.rep_kinds or []),
        "rep_categories": list(user.rep_categories or []),
    }
    if user.photo_path:
        out["photo"] = get_storage().get_url("product-images", user.photo_path, request=request)
    if user.profile_id:
        out["profile_id"] = user.profile_id
    if user.sales_target is not None:
        out["sales_target"] = int(user.sales_target)
    return out


def serialize_warehouse(wh: Warehouse) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": wh.id,
        "name": wh.name,
        "name_en": wh.name_en,
        "code": wh.code,
        "capacity": wh.capacity,
        "active": bool(wh.active),
    }
    if wh.lat is not None and wh.lng is not None:
        out["lat"] = float(wh.lat)
        out["lng"] = float(wh.lng)
    if wh.layout is not None:
        out["layout"] = wh.layout
    if isinstance(wh.cover, dict):
        now_iso = timezone.now().isoformat()
        if str(wh.cover.get("until") or "") > now_iso:
            out["cover"] = wh.cover
    return out


def serialize_vehicle(v: Any) -> dict[str, Any]:
    return {
        "id": v.id,
        "name": v.name,
        "plate": v.plate or "",
        "capacity_kg": int(v.capacity_kg),
        "driver_id": v.driver_id,
        "active": bool(v.active),
    }


def serialize_tier(tier: PriceTier) -> dict[str, Any]:
    return {
        "id": tier.id,
        "name": tier.name,
        "name_en": tier.name_en,
    }


def serialize_settings(s: SystemSetting) -> dict[str, Any]:
    from apps.accounts.business_rules import changed_rules, rules_of

    usd = s.usd_rate
    usd_out: int | float = int(usd) if usd == int(usd) else float(usd)
    pct = s.commission_pct
    pct_out: int | float = int(pct) if pct == int(pct) else float(pct)
    out = {
        "reservation_hours": s.reservation_hours,
        "near_expiry_days": s.near_expiry_days,
        "slow_moving_days": s.slow_moving_days,
        "auto_confirm_rep_orders": bool(s.auto_confirm_rep_orders),
        "commission_pct": pct_out,
        "usd_rate": usd_out,
        "phone": s.phone,
        "address": s.address,
        "expense_approval_above": (
            int(s.expense_approval_above) if s.expense_approval_above is not None else 1_000_000
        ),
        "summary_hour": int(getattr(s, "summary_hour", 21) if getattr(s, "summary_hour", None) is not None else 21),
    }
    adv = changed_rules(rules_of(s))
    if adv:
        out["advanced"] = adv
    return out


def validate_rep_kinds(raw: Any) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("rep_kinds must be a list")
    cleaned = []
    for k in raw:
        if k not in ("sales", "delivery"):
            raise ValueError("Invalid rep_kind value")
        if k not in cleaned:
            cleaned.append(k)
    return cleaned


def decimal_from_num(val: Any) -> Decimal:
    return Decimal(str(val))


# ==========================================
# DRF Serializers for OpenAPI documentation
# ==========================================


class UserSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    name = serializers.CharField()
    phone = serializers.CharField()
    role = serializers.CharField()
    active = serializers.BooleanField()
    warehouse_id = serializers.IntegerField(allow_null=True, required=False)
    customer_id = serializers.IntegerField(allow_null=True, required=False)
    customer_ids = serializers.ListField(child=serializers.IntegerField(), required=False)
    rep_kinds = serializers.ListField(child=serializers.CharField(), required=False)
    rep_categories = serializers.ListField(child=serializers.CharField(), required=False)
    photo = serializers.CharField(required=False, allow_null=True)
    profile_id = serializers.CharField(required=False, allow_null=True)
    sales_target = serializers.IntegerField(required=False, allow_null=True)


class WarehouseSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    name = serializers.CharField()
    name_en = serializers.CharField(allow_blank=True)
    code = serializers.CharField()
    capacity = serializers.IntegerField()
    lat = serializers.FloatField(allow_null=True, required=False)
    lng = serializers.FloatField(allow_null=True, required=False)
    active = serializers.BooleanField()
    layout = serializers.JSONField(required=False, allow_null=True)
    cover = serializers.JSONField(required=False, allow_null=True)


class VehicleSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    name = serializers.CharField()
    plate = serializers.CharField(allow_blank=True)
    capacity_kg = serializers.IntegerField()
    driver_id = serializers.IntegerField(allow_null=True)
    active = serializers.BooleanField()


class PriceTierSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    name = serializers.CharField()
    name_en = serializers.CharField(allow_blank=True)


class SettingsSerializer(serializers.Serializer):
    reservation_hours = serializers.IntegerField()
    near_expiry_days = serializers.IntegerField()
    slow_moving_days = serializers.IntegerField()
    auto_confirm_rep_orders = serializers.BooleanField()
    commission_pct = serializers.FloatField()
    usd_rate = serializers.FloatField()
    phone = serializers.CharField(allow_blank=True)
    address = serializers.CharField(allow_blank=True)
    expense_approval_above = serializers.IntegerField()
    summary_hour = serializers.IntegerField(required=False)
    advanced = serializers.DictField(required=False)
    how = serializers.ChoiceField(choices=["import", "reset"], required=False)


class BrandSerializer(serializers.Serializer):
    name = serializers.CharField()
    name_en = serializers.CharField()
    tagline = serializers.CharField()
    tagline_en = serializers.CharField()
    company = serializers.CharField()
    company_en = serializers.CharField()
    phone = serializers.CharField(allow_blank=True)
    logo = serializers.CharField(required=False, allow_null=True, allow_blank=True)


class ShopBannerLinkSerializer(serializers.Serializer):
    kind = serializers.ChoiceField(choices=["category", "brand", "item", "search"])
    value = serializers.CharField()


class ShopBannerSerializer(serializers.Serializer):
    id = serializers.CharField()
    image = serializers.CharField()
    title = serializers.CharField()
    title_en = serializers.CharField(required=False, allow_blank=True)
    sub = serializers.CharField(required=False, allow_blank=True)
    link = ShopBannerLinkSerializer()
    active = serializers.BooleanField()


class SiteCustomSerializer(serializers.Serializer):
    brand = BrandSerializer()
    nav = serializers.JSONField()
    dashboard = serializers.JSONField()
    categories = serializers.ListField(child=serializers.DictField())
    texts = serializers.DictField()
    layouts = serializers.DictField()
    look = serializers.DictField(required=False)
    profiles = serializers.ListField(child=serializers.DictField(), required=False)
    apps = serializers.ListField(child=serializers.DictField(), required=False)
    fields = serializers.ListField(child=serializers.DictField(), required=False)
    rules = serializers.ListField(child=serializers.DictField(), required=False)
    banners = ShopBannerSerializer(many=True, required=False)


class LoginInputSerializer(serializers.Serializer):
    phone = serializers.CharField()
    password = serializers.CharField()


class RefreshInputSerializer(serializers.Serializer):
    refresh = serializers.CharField(required=False, allow_blank=True)


class AuthTokensSerializer(serializers.Serializer):
    token = serializers.CharField()
    refresh = serializers.CharField(required=False, allow_blank=True)
    user = UserSerializer()


class ChangePasswordInputSerializer(serializers.Serializer):
    old_password = serializers.CharField(required=False, allow_blank=True)
    new_password = serializers.CharField()


class CreateUserInputSerializer(serializers.Serializer):
    name = serializers.CharField()
    phone = serializers.CharField()
    role = serializers.CharField()
    password = serializers.CharField(required=False, allow_blank=True)
    warehouse_id = serializers.IntegerField(required=False, allow_null=True)
    rep_kinds = serializers.ListField(child=serializers.CharField(), required=False)
    rep_categories = serializers.ListField(child=serializers.CharField(), required=False)
    profile_id = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    sales_target = serializers.IntegerField(required=False, allow_null=True)


class UpdateUserInputSerializer(serializers.Serializer):
    name = serializers.CharField(required=False)
    phone = serializers.CharField(required=False)
    role = serializers.CharField(required=False)
    active = serializers.BooleanField(required=False)
    password = serializers.CharField(required=False, allow_blank=True)
    warehouse_id = serializers.IntegerField(required=False, allow_null=True)
    rep_kinds = serializers.ListField(child=serializers.CharField(), required=False)
    rep_categories = serializers.ListField(child=serializers.CharField(), required=False)
    profile_id = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    sales_target = serializers.IntegerField(required=False, allow_null=True)
