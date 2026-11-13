"""Central RBAC permission classes and data visibility helpers."""
from __future__ import annotations

from typing import Any

from rest_framework.permissions import BasePermission

from .models import COST_ROLES, DEBT_ROLES, PROFIT_ROLES, Role


def role_permission(*allowed_roles: str) -> type[BasePermission]:
    """Factory returning a DRF BasePermission checking actor role & active flag."""
    allowed = frozenset(allowed_roles)

    class _RolePermission(BasePermission):
        def has_permission(self, request, view) -> bool:
            user = getattr(request, "user", None)
            return bool(
                user
                and user.is_authenticated
                and getattr(user, "active", False)
                and not getattr(user, "deleted", False)
                and getattr(user, "role", None) in allowed
            )

    _RolePermission.__name__ = f"RolePermission_{'_'.join(sorted(allowed))}"
    return _RolePermission


def is_manager(user: Any) -> bool:
    return bool(user and getattr(user, "role", None) == Role.MANAGER)


def sees_cost(user: Any) -> bool:
    return bool(user and getattr(user, "role", None) in COST_ROLES)


def sees_profit(user: Any) -> bool:
    if not user or getattr(user, "role", None) not in PROFIT_ROLES:
        return False
    from apps.accounts.profiles import limits_of

    return not bool(limits_of(user).get("no_profit"))


def sees_debt(user: Any) -> bool:
    if not user or getattr(user, "role", None) not in DEBT_ROLES:
        return False
    from apps.accounts.profiles import limits_of

    return not bool(limits_of(user).get("no_debt"))


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
