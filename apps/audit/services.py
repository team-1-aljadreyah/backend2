"""Audit recording and read/serialization helpers (append-only, never deleted)."""
from __future__ import annotations

from typing import Any

from rest_framework import status as http_status

from apps.accounts.models import Role, User
from apps.common.exceptions import ApiException, ErrorCode

from .models import AuditLog

_VALID_ACTIONS = frozenset(
    {"create", "update", "cancel", "reverse", "transfer", "price_change", "approve", "confirm", "close", "login"}
)


def record_audit(
    *,
    actor: Any = None,
    action: str,
    entity_type: str,
    entity_id: int | str,
    summary: str = "",
    summary_ar: str = "",
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    ip_address: str | None = None,
) -> AuditLog:
    is_auth = bool(actor and getattr(actor, "is_authenticated", False))
    return AuditLog.objects.create(
        actor=actor if is_auth else None,
        actor_name=getattr(actor, "name", "") if is_auth else "system",
        actor_role=getattr(actor, "role", "") if is_auth else "system",
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id),
        summary=summary[:300],
        summary_ar=summary_ar[:300],
        before=before,
        after=after,
        ip_address=ip_address,
    )


def _normalize_audit_action(raw: str) -> str:
    val = (raw or "").strip().lower()
    if val in _VALID_ACTIONS:
        return val
    if "price" in val:
        return "price_change"
    if "reverse" in val:
        return "reverse"
    if "cancel" in val:
        return "cancel"
    if "transfer" in val:
        return "transfer"
    if "approve" in val or "accept" in val:
        return "approve"
    if "confirm" in val:
        return "confirm"
    if "close" in val:
        return "close"
    if "login" in val:
        return "login"
    if "create" in val:
        return "create"
    return "update"


def serialize_audit(entry: AuditLog) -> dict[str, Any]:
    return {
        "id": entry.id,
        "at": entry.created_at.isoformat(),
        "action": _normalize_audit_action(entry.action),
        "raw_action": entry.action,
        "entity": entry.entity_type,
        "entity_id": entry.entity_id,
        "user": {
            "id": entry.actor_id or 0,
            "name": entry.actor_name or "System",
            "role": entry.actor_role or "system",
        },
        "summary": entry.summary_ar or entry.summary or entry.action,
        "summary_en": entry.summary or entry.summary_ar or entry.action,
        "before": entry.before,
        "after": entry.after,
    }


def list_audit_for_actor(
    *,
    actor: User,
    limit: int = 300,
    action: str | None = None,
    entity: str | None = None,
) -> list[dict[str, Any]]:
    if actor.role not in (Role.MANAGER, Role.ACCOUNTANT):
        raise ApiException(code=ErrorCode.FORBIDDEN, status_code=http_status.HTTP_403_FORBIDDEN)

    qs = AuditLog.objects.order_by("-id")
    if entity:
        qs = qs.filter(entity_type=entity.strip())

    cap = max(1, min(limit, 500))
    rows = []
    for entry in qs[: cap * 2 if action else cap]:
        serialized = serialize_audit(entry)
        if action and serialized["action"] != action.strip():
            continue
        rows.append(serialized)
        if len(rows) >= cap:
            break
    return rows
