"""Central RBAC permission classes and data visibility helpers."""
from __future__ import annotations

from typing import Any

from rest_framework.permissions import BasePermission

from apps.common.exceptions import ApiException, ErrorCode

from .business_rules import ACTIONS, rules_of, user_may_in_rules
from .models import Role


def _active_user(user: Any) -> bool:
    return bool(
        user
        and getattr(user, "is_authenticated", True)
        and getattr(user, "active", False)
        and not getattr(user, "deleted", False)
    )


def role_permission(*allowed_roles: str) -> type[BasePermission]:
    """Factory returning a DRF BasePermission checking actor role & active flag."""
    allowed = frozenset(allowed_roles)

    class _RolePermission(BasePermission):
        def has_permission(self, request, view) -> bool:
            user = getattr(request, "user", None)
            return bool(_active_user(user) and getattr(user, "role", None) in allowed)

    _RolePermission.__name__ = f"RolePermission_{'_'.join(sorted(allowed))}"
    return _RolePermission


def user_may(
    user: Any,
    action_key: str,
    *,
    setting: Any = None,
    rules: dict[str, Any] | None = None,
) -> bool:
    """Return True if actor may perform action_key under current rules & people overrides."""
    if not _active_user(user):
        return False
    r = rules if rules is not None else rules_of(setting)
    return user_may_in_rules(r, user, action_key)


def require_action(
    user: Any,
    action_key: str,
    *,
    setting: Any = None,
    rules: dict[str, Any] | None = None,
) -> None:
    """Raise 403 FORBIDDEN if actor is not allowed to perform action_key."""
    if user_may(user, action_key, setting=setting, rules=rules):
        return
    meta = ACTIONS.get(action_key, {})
    ar = meta.get("ar", action_key)
    en = meta.get("en", action_key)
    raise ApiException(
        ErrorCode.FORBIDDEN,
        (f"ليس من صلاحياتك: {ar}", f"Not allowed: {en}"),
        status_code=403,
    )


def action_permission(action_key: str) -> type[BasePermission]:
    """Factory returning a DRF BasePermission enforcing dynamic `permissions` / `people` rules."""

    class _ActionPermission(BasePermission):
        def has_permission(self, request, view) -> bool:
            user = getattr(request, "user", None)
            if not user or not getattr(user, "is_authenticated", False):
                return False
            if not getattr(user, "active", False) or getattr(user, "deleted", False):
                return False
            require_action(user, action_key)
            return True

    _ActionPermission.__name__ = f"ActionPermission_{action_key}"
    return _ActionPermission


def is_manager(user: Any) -> bool:
    return bool(user and getattr(user, "role", None) == Role.MANAGER)


def sees_cost(user: Any) -> bool:
    if not _active_user(user) or getattr(user, "role", None) == Role.SHOP:
        return False
    if getattr(user, "role", None) == Role.WAREHOUSE:
        return True
    from apps.accounts.profiles import limits_of

    return user_may(user, "see_costs") and not bool(limits_of(user).get("no_profit"))


def sees_profit(user: Any) -> bool:
    if not _active_user(user) or getattr(user, "role", None) == Role.SHOP:
        return False
    from apps.accounts.profiles import limits_of

    return user_may(user, "see_costs") and not bool(limits_of(user).get("no_profit"))


def sees_debt(user: Any) -> bool:
    if not _active_user(user):
        return False
    from apps.accounts.profiles import limits_of

    if getattr(user, "role", None) == Role.SHOP:
        return not bool(limits_of(user).get("no_debt"))
    return user_may(user, "see_debts") and not bool(limits_of(user).get("no_debt"))


def own_warehouse_id(user: Any) -> int | None:
    if user and getattr(user, "role", None) in (Role.WAREHOUSE, Role.STAFF):
        return getattr(user, "warehouse_id", None)
    return None


def covering_warehouse_ids(user: Any) -> list[int]:
    """Return active covered warehouse IDs for a warehouse keeper."""
    if not user or getattr(user, "role", None) != Role.WAREHOUSE:
        return []
    cached = getattr(user, "_cached_covering_wh_ids", ...)
    if cached is not ...:
        return cached
    from django.utils import timezone

    from apps.warehouses.models import Warehouse

    now_iso = timezone.now().isoformat()
    ids: list[int] = []
    for wh in Warehouse.objects.filter(active=True).exclude(cover__isnull=True).only("id", "cover"):
        cov = wh.cover
        if isinstance(cov, dict) and cov.get("user_id") == user.id and str(cov.get("until") or "") > now_iso:
            ids.append(wh.id)
    user._cached_covering_wh_ids = ids
    return ids


def works_in_warehouse(user: Any, warehouse_id: int | None) -> bool:
    if not user:
        return False
    if getattr(user, "role", None) not in (Role.WAREHOUSE, Role.STAFF):
        return True
    if warehouse_id is None:
        return False
    if getattr(user, "warehouse_id", None) == warehouse_id:
        return True
    return warehouse_id in covering_warehouse_ids(user)


def rep_may_sell(user: Any) -> bool:
    return bool(user and getattr(user, "role", None) == Role.REP and user.has_rep_kind("sales"))


def rep_may_deliver(user: Any) -> bool:
    return bool(user and getattr(user, "role", None) == Role.REP and user.has_rep_kind("delivery"))


def get_active_customer_id(user: Any) -> int | None:
    if not user or getattr(user, "role", None) != Role.SHOP:
        return None
    cached = getattr(user, "_cached_active_customer_id", ...)
    if cached is not ...:
        return cached
    sess_cid = getattr(user, "_session_active_customer_id", ...)
    if sess_cid is not ... and sess_cid is not None:
        user._cached_active_customer_id = sess_cid
        return sess_cid
    from apps.customers.models import ShopMembership

    cid = (
        ShopMembership.objects.filter(user=user, is_active_shop=True)
        .values_list("customer_id", flat=True)
        .first()
    )
    user._cached_active_customer_id = cid
    return cid
