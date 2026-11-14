"""Notification & outbox emission services."""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from django.utils import timezone

from apps.accounts.business_rules import DEFAULT_NOTIFY, canonical_notify_kind, rules_of
from apps.realtime.broadcaster import publish_notification

from .models import Notification, OutboxEvent

# Mirrors frontend `components/nav.ts` (most specific prefix wins; kind="sales"|"delivery" applies only to role=="rep").
_FRONTEND_PATH_RULES: tuple[tuple[str, frozenset[str], str | None], ...] = (
    ("/payments/", frozenset({"manager", "accountant", "rep"}), None),
    ("/items/", frozenset({"manager", "warehouse", "accountant", "staff"}), None),
    ("/invoices/", frozenset({"manager", "accountant", "rep", "shop", "warehouse"}), None),
    ("/returns/", frozenset({"manager", "accountant", "rep", "warehouse", "shop"}), None),
    ("/notifications", frozenset({"manager", "warehouse", "rep", "accountant", "shop", "staff"}), None),
    ("/orders", frozenset({"manager", "warehouse", "rep", "accountant", "shop", "staff"}), None),
    ("/deliveries", frozenset({"rep", "manager", "warehouse", "accountant", "staff"}), "delivery"),
    ("/customers", frozenset({"manager", "accountant", "rep"}), "sales"),
    ("/commissions", frozenset({"manager", "accountant", "rep"}), "sales"),
    ("/custody", frozenset({"manager", "accountant", "rep"}), "sales"),
    ("/expenses", frozenset({"accountant", "manager"}), None),
    ("/statement", frozenset({"shop"}), None),
    ("/account", frozenset({"shop"}), None),
    ("/showcase", frozenset({"shop"}), None),
    ("/shop", frozenset({"shop"}), None),
    ("/invoices", frozenset({"manager", "accountant", "rep", "shop"}), None),
    ("/returns", frozenset({"manager", "accountant", "rep", "warehouse"}), None),
    ("/payments", frozenset({"manager", "accountant", "rep"}), None),
    ("/items", frozenset({"manager", "warehouse", "accountant", "rep", "staff"}), None),
    ("/stock", frozenset({"manager", "warehouse", "accountant", "staff"}), None),
    ("/supply", frozenset({"manager", "accountant", "warehouse", "staff"}), None),
    ("/transfers", frozenset({"manager", "warehouse"}), None),
    ("/stocktakes", frozenset({"manager", "warehouse"}), None),
    ("/hall", frozenset({"warehouse"}), None),
    ("/support", frozenset({"staff", "manager"}), None),
    ("/settings", frozenset({"manager"}), None),
)


def _can_open_frontend_path(
    path: str,
    *,
    role: str,
    rep_kinds: list[str] | tuple[str, ...] | None = None,
    denied: list[str] | tuple[str, ...] | None = None,
) -> bool:
    """Return True iff `role` (and rep_kinds / profile denied list) may open `path` in the frontend."""
    clean = path.split("?", 1)[0].split("#", 1)[0]
    matched: list[tuple[str, frozenset[str], str | None]] = []
    for prefix, roles, kind in _FRONTEND_PATH_RULES:
        if clean == prefix or clean.startswith(prefix if prefix.endswith("/") else prefix + "/"):
            matched.append((prefix, roles, kind))
    if not matched:
        return True
    matched.sort(key=lambda r: len(r[0]), reverse=True)
    prefix, roles, kind = matched[0]
    if denied and prefix.rstrip("/") in denied:
        return False
    if role not in roles:
        return False
    if kind and role == "rep":
        kinds = [k for k in (rep_kinds or []) if k]
        # Empty rep_kinds defaults to both kinds in legacy rep accounts.
        if kinds and kind not in kinds:
            return False
    return True


def derive_notification_link(
    ref_type: str,
    ref_id: str | int = "",
    *,
    actor: Any = None,
    fallback_role: str = "",
) -> str | None:
    """Derive a frontend route for a notification that the receiving user is allowed to open."""
    if not ref_type:
        return None
    rid = str(ref_id) if ref_id not in (None, "") else ""

    role = ""
    rep_kinds: list[str] = []
    denied: list[str] = []
    if actor is not None:
        role = str(getattr(actor, "role", "") or "")
        raw_kinds = getattr(actor, "rep_kinds", None)
        if isinstance(raw_kinds, (list, tuple)):
            rep_kinds = [str(k) for k in raw_kinds if k]
        try:
            from apps.accounts.profiles import profile_of

            prof = profile_of(actor)
            if isinstance(prof, dict) and isinstance(prof.get("denied"), list):
                denied = [str(p) for p in prof["denied"] if isinstance(p, str)]
        except Exception:
            pass
    elif fallback_role:
        role = str(fallback_role)

    candidates: list[str] = []
    if ref_type == "order":
        candidates = [f"/orders/{rid}"] if rid else ["/orders"]
    elif ref_type == "invoice":
        candidates = [f"/invoices/{rid}"] if rid else ["/invoices"]
    elif ref_type == "payment":
        candidates = ["/statement"] if role == "shop" else ([f"/payments/{rid}"] if rid else ["/payments"])
    elif ref_type == "refund":
        candidates = ["/statement", "/payments"]
    elif ref_type == "return":
        candidates = [f"/returns/{rid}"] if rid else ["/returns"]
    elif ref_type == "customer":
        candidates = ["/account"] if role == "shop" else ([f"/customers/{rid}"] if rid else ["/customers"])
    elif ref_type == "item":
        candidates = ["/showcase"] if role == "shop" else ([f"/items/{rid}", "/items"] if rid else ["/items"])
    elif ref_type == "catalog":
        candidates = ["/showcase", "/items"]
    elif ref_type == "batch":
        candidates = ["/stock"]
    elif ref_type == "delivery":
        candidates = ["/deliveries", f"/orders/{rid}"] if rid else ["/deliveries"]
    elif ref_type == "custody":
        candidates = ["/custody"]
    elif ref_type == "supply_order":
        candidates = [f"/supply?open={rid}"] if rid else ["/supply"]
    elif ref_type == "transfer":
        candidates = ["/transfers"]
    elif ref_type == "stocktake":
        candidates = [f"/stocktakes/{rid}"] if rid else ["/stocktakes"]
    elif ref_type == "warehouse":
        candidates = ["/hall", "/stock"]
    elif ref_type == "expense":
        candidates = [f"/expenses/{rid}"] if rid else ["/expenses"]
    elif ref_type == "commission":
        candidates = ["/commissions"]
    elif ref_type == "settings":
        candidates = ["/settings"]
    elif ref_type == "chat":
        candidates = ["/shop", f"/support?shop={rid}" if rid else "/support"]

    if not candidates:
        return None
    if not role:
        return candidates[0]
    for path in candidates:
        if _can_open_frontend_path(path, role=role, rep_kinds=rep_kinds, denied=denied):
            return path
    return None


def _notify_matrix_roles(kind: str) -> tuple[str, list[str]] | None:
    """Return `(canonical_kind, allowed_roles)` if `notify_matrix` overrides `kind`, else None."""
    matrix = rules_of().get("notify_matrix") or {}
    if not isinstance(matrix, dict) or not matrix:
        return None
    canonical = canonical_notify_kind(kind)
    if kind in matrix and isinstance(matrix[kind], list):
        return canonical, [str(r) for r in matrix[kind] if isinstance(r, str)]
    if canonical in matrix and isinstance(matrix[canonical], list):
        return canonical, [str(r) for r in matrix[canonical] if isinstance(r, str)]
    return None


def _create_role_notification(
    *,
    role: str,
    kind: str,
    title_ar: str,
    title_en: str,
    body_ar: str = "",
    body_en: str = "",
    ref_type: str = "",
    ref_id: int | str = "",
) -> Notification:
    notif = Notification.objects.create(
        target_role=role,
        kind=kind,
        title_ar=title_ar[:255],
        title_en=title_en[:255],
        body_ar=body_ar,
        body_en=body_en,
        ref_type=ref_type,
        ref_id=str(ref_id) if ref_id != "" else "",
    )
    publish_notification(notif)
    try:
        from .push import dispatch_push_to_role

        url = derive_notification_link(ref_type, ref_id, fallback_role=role) or ""
        dispatch_push_to_role(role, title=title_ar, body=body_ar or title_en, url=url, tag=kind)
    except Exception:
        pass
    return notif


def _dispatch_notify_matrix_extras(
    *,
    canonical: str,
    allowed_roles: list[str],
    already_roles: set[str],
    kind: str,
    title_ar: str,
    title_en: str,
    body_ar: str,
    body_en: str,
    ref_type: str,
    ref_id: int | str,
) -> None:
    default_roles = set(DEFAULT_NOTIFY.get(canonical, []))
    rid_str = str(ref_id) if ref_id != "" else ""
    cutoff = timezone.now() - timedelta(seconds=10)
    for extra_role in allowed_roles:
        if extra_role in default_roles or extra_role in already_roles:
            continue
        dup_qs = Notification.objects.filter(
            target_role=extra_role,
            kind=kind,
            created_at__gte=cutoff,
        )
        if ref_type and rid_str:
            dup_qs = dup_qs.filter(ref_type=ref_type, ref_id=rid_str)
        else:
            dup_qs = dup_qs.filter(title_ar=title_ar[:255])
        if dup_qs.exists():
            continue
        _create_role_notification(
            role=extra_role,
            kind=kind,
            title_ar=title_ar,
            title_en=title_en,
            body_ar=body_ar,
            body_en=body_en,
            ref_type=ref_type,
            ref_id=ref_id,
        )


def notify_user(
    *,
    user_id: int,
    kind: str,
    title_ar: str,
    title_en: str,
    body_ar: str = "",
    body_en: str = "",
    ref_type: str = "",
    ref_id: int | str = "",
) -> Notification | None:
    from apps.accounts.models import User

    target_u = User.objects.filter(id=user_id).first()
    override = _notify_matrix_roles(kind)
    if override is not None:
        canonical, allowed = override
        u_role = target_u.role if target_u else ""
        _dispatch_notify_matrix_extras(
            canonical=canonical,
            allowed_roles=allowed,
            already_roles={u_role} if u_role else set(),
            kind=kind,
            title_ar=title_ar,
            title_en=title_en,
            body_ar=body_ar,
            body_en=body_en,
            ref_type=ref_type,
            ref_id=ref_id,
        )
        if not u_role or u_role not in allowed:
            return None

    notif = Notification.objects.create(
        user_id=user_id,
        kind=kind,
        title_ar=title_ar[:255],
        title_en=title_en[:255],
        body_ar=body_ar,
        body_en=body_en,
        ref_type=ref_type,
        ref_id=str(ref_id) if ref_id != "" else "",
    )
    publish_notification(notif)
    try:
        from .push import dispatch_push_to_user

        if target_u:
            url = derive_notification_link(ref_type, ref_id, actor=target_u) or ""
            dispatch_push_to_user(target_u, title=title_ar, body=body_ar or title_en, url=url, tag=kind)
    except Exception:
        pass
    return notif


def notify_role(
    *,
    role: str,
    kind: str,
    title_ar: str,
    title_en: str,
    body_ar: str = "",
    body_en: str = "",
    ref_type: str = "",
    ref_id: int | str = "",
) -> Notification | None:
    override = _notify_matrix_roles(kind)
    if override is not None:
        canonical, allowed = override
        _dispatch_notify_matrix_extras(
            canonical=canonical,
            allowed_roles=allowed,
            already_roles={role},
            kind=kind,
            title_ar=title_ar,
            title_en=title_en,
            body_ar=body_ar,
            body_en=body_en,
            ref_type=ref_type,
            ref_id=ref_id,
        )
        if role not in allowed:
            return None

    return _create_role_notification(
        role=role,
        kind=kind,
        title_ar=title_ar,
        title_en=title_en,
        body_ar=body_ar,
        body_en=body_en,
        ref_type=ref_type,
        ref_id=ref_id,
    )


def notify_customer(
    *,
    customer_id: int,
    kind: str,
    title_ar: str,
    title_en: str,
    body_ar: str = "",
    body_en: str = "",
    ref_type: str = "",
    ref_id: int | str = "",
) -> list[Notification]:
    from django.apps import apps

    if not apps.is_installed("apps.customers"):
        return []
    override = _notify_matrix_roles(kind)
    if override is not None:
        canonical, allowed = override
        _dispatch_notify_matrix_extras(
            canonical=canonical,
            allowed_roles=allowed,
            already_roles={"shop"},
            kind=kind,
            title_ar=title_ar,
            title_en=title_en,
            body_ar=body_ar,
            body_en=body_en,
            ref_type=ref_type,
            ref_id=ref_id,
        )
        if "shop" not in allowed:
            return []

    from apps.customers.models import ShopMembership

    user_ids = list(
        ShopMembership.objects.filter(customer_id=customer_id, user__active=True)
        .values_list("user_id", flat=True)
        .distinct()
    )
    out: list[Notification] = []
    for uid in user_ids:
        n = notify_user(
            user_id=uid,
            kind=kind,
            title_ar=title_ar,
            title_en=title_en,
            body_ar=body_ar,
            body_en=body_en,
            ref_type=ref_type,
            ref_id=ref_id,
        )
        if n is not None:
            out.append(n)
    return out


def enqueue_outbox(topic: str, payload: dict[str, Any]) -> OutboxEvent:
    return OutboxEvent.objects.create(topic=topic, payload=payload)
