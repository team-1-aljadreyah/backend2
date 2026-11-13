"""Support chat business services with RBAC scoping and read cursors."""
from __future__ import annotations

from typing import Any

from django.db import transaction
from django.db.models import Max
from rest_framework import status as http_status

from apps.accounts.models import Role, User
from apps.accounts.permissions import get_active_customer_id
from apps.audit.services import record_audit
from apps.common.exceptions import ApiException, ErrorCode
from apps.customers.models import Customer
from apps.notifications.services import notify_customer, notify_role
from apps.realtime.broadcaster import publish_chat_message

from .models import ChatMessage, ChatReadCursor, ChatSenderKind

MAX_MESSAGE_LENGTH = 1000
_COMPANY_CHAT_ROLES = (Role.STAFF, Role.MANAGER)


def serialize_chat_message(msg: ChatMessage) -> dict[str, Any]:
    return {
        "id": msg.id,
        "customer_id": msg.customer_id,
        "from": msg.sender_kind,
        "by": {
            "id": msg.sender_id,
            "name": msg.sender.name,
            "role": msg.sender.role,
        },
        "text": msg.text,
        "at": msg.created_at.isoformat(),
    }


def _validate_message_text(text: str) -> str:
    clean = (text or "").strip()
    if not clean:
        raise ApiException(
            code=ErrorCode.VALIDATION,
            message=("نص الرسالة مطلوب", "Message text is required"),
            status_code=http_status.HTTP_400_BAD_REQUEST,
            errors={"text": ("مطلوب", "Required")},
        )
    if len(clean) > MAX_MESSAGE_LENGTH:
        raise ApiException(
            code=ErrorCode.VALIDATION,
            message=("الرسالة طويلة جداً", f"Message exceeds {MAX_MESSAGE_LENGTH} characters"),
            status_code=http_status.HTTP_400_BAD_REQUEST,
            errors={"text": ("طويل جداً", "Too long")},
        )
    return clean


def _require_customer_for_actor(actor: User, customer_id: int) -> Customer:
    customer = Customer.objects.filter(id=customer_id).first()
    if not customer:
        raise ApiException(code=ErrorCode.NOT_FOUND, status_code=http_status.HTTP_404_NOT_FOUND)

    if actor.role == Role.SHOP:
        if customer.id != get_active_customer_id(actor):
            raise ApiException(code=ErrorCode.FORBIDDEN, status_code=http_status.HTTP_403_FORBIDDEN)
        return customer

    if actor.role in _COMPANY_CHAT_ROLES:
        return customer

    raise ApiException(code=ErrorCode.FORBIDDEN, status_code=http_status.HTTP_403_FORBIDDEN)


def _unread_shop_side(*, user_id: int, customer_id: int) -> int:
    last_id = (
        ChatReadCursor.objects.filter(user_id=user_id, customer_id=customer_id)
        .values_list("last_read_message_id", flat=True)
        .first()
        or 0
    )
    return ChatMessage.objects.filter(
        customer_id=customer_id,
        sender_kind=ChatSenderKind.STAFF,
        id__gt=last_id,
    ).count()


def get_my_chat(*, actor: User) -> dict[str, Any]:
    if actor.role != Role.SHOP:
        raise ApiException(code=ErrorCode.FORBIDDEN, status_code=http_status.HTTP_403_FORBIDDEN)
    customer_id = get_active_customer_id(actor)
    if not customer_id:
        raise ApiException(code=ErrorCode.FORBIDDEN, status_code=http_status.HTTP_403_FORBIDDEN)

    messages = list(
        ChatMessage.objects.select_related("sender")
        .filter(customer_id=customer_id)
        .order_by("id")[:500]
    )
    unread = _unread_shop_side(user_id=actor.id, customer_id=customer_id)
    return {
        "messages": [serialize_chat_message(m) for m in messages],
        "unread": unread,
    }


@transaction.atomic
def send_my_chat_message(*, actor: User, text: str) -> dict[str, Any]:
    if actor.role != Role.SHOP:
        raise ApiException(code=ErrorCode.FORBIDDEN, status_code=http_status.HTTP_403_FORBIDDEN)
    customer_id = get_active_customer_id(actor)
    if not customer_id:
        raise ApiException(code=ErrorCode.FORBIDDEN, status_code=http_status.HTTP_403_FORBIDDEN)

    clean = _validate_message_text(text)
    customer = Customer.objects.get(id=customer_id)
    prev = ChatMessage.objects.filter(customer=customer).order_by("-id").first()
    msg = ChatMessage.objects.create(
        customer=customer,
        sender=actor,
        sender_kind=ChatSenderKind.SHOP,
        text=clean,
    )
    ChatReadCursor.objects.update_or_create(
        user=actor,
        customer=customer,
        defaults={"last_read_message": msg},
    )
    # Notify support staff & manager when a thread begins waiting (matches rules.ts)
    if not prev or prev.sender_kind == ChatSenderKind.STAFF:
        for role in _COMPANY_CHAT_ROLES:
            notify_role(
                role=role,
                kind="support_message",
                title_ar=f"رسالة دعم جديدة من {customer.name}",
                title_en=f"New support message from {customer.name}",
                body_ar=clean[:120],
                body_en=clean[:120],
                ref_type="chat",
                ref_id=str(customer.id),
            )
    record_audit(
        actor=actor,
        action="create",
        entity_type="chat",
        entity_id=msg.id,
        summary=f"Support message from {customer.name}",
        summary_ar=f"رسالة دعم من {customer.name}",
    )
    publish_chat_message(customer.id)
    return serialize_chat_message(msg)


def list_chat_threads(*, actor: User) -> list[dict[str, Any]]:
    if actor.role not in _COMPANY_CHAT_ROLES:
        raise ApiException(code=ErrorCode.FORBIDDEN, status_code=http_status.HTTP_403_FORBIDDEN)

    latest_rows = list(
        ChatMessage.objects.values("customer_id")
        .annotate(last_id=Max("id"))
        .order_by("-last_id")[:200]
    )
    if not latest_rows:
        return []

    last_ids = [r["last_id"] for r in latest_rows]
    cids = [r["customer_id"] for r in latest_rows]
    last_msgs = {
        m.customer_id: m
        for m in ChatMessage.objects.select_related("customer", "sender").filter(id__in=last_ids)
    }
    # Last staff answer per customer
    last_staff_ids = (
        ChatMessage.objects.filter(customer_id__in=cids, sender_kind=ChatSenderKind.STAFF)
        .values("customer_id")
        .annotate(last_staff_id=Max("id"))
    )
    staff_msg_by_cid = {
        m.customer_id: m
        for m in ChatMessage.objects.select_related("sender").filter(
            id__in=[r["last_staff_id"] for r in last_staff_ids]
        )
    }

    # Company-side cursor: max read message id across support roles
    staff_cursors = {
        row["customer_id"]: (row["max_read"] or 0)
        for row in ChatReadCursor.objects.filter(
            customer_id__in=cids, user__role__in=_COMPANY_CHAT_ROLES
        )
        .values("customer_id")
        .annotate(max_read=Max("last_read_message_id"))
    }

    unread_map: dict[int, int] = {}
    for cid, mid in ChatMessage.objects.filter(
        customer_id__in=cids, sender_kind=ChatSenderKind.SHOP
    ).values_list("customer_id", "id"):
        if mid > staff_cursors.get(cid, 0):
            unread_map[cid] = unread_map.get(cid, 0) + 1

    threads: list[dict[str, Any]] = []
    for row in latest_rows:
        msg = last_msgs.get(row["customer_id"])
        if not msg:
            continue
        cust = msg.customer
        ans = staff_msg_by_cid.get(cust.id)
        entry: dict[str, Any] = {
            "customer_id": cust.id,
            "customer": cust.name,
            "area": cust.area,
            "last": serialize_chat_message(msg),
            "waiting": msg.sender_kind == ChatSenderKind.SHOP,
            "unread": unread_map.get(cust.id, 0),
        }
        if ans is not None:
            entry["answered_by"] = {"id": ans.sender_id, "name": ans.sender.name}
        threads.append(entry)

    threads.sort(key=lambda t: (t["waiting"], t["last"]["at"]), reverse=True)
    return threads


def list_customer_chat(*, actor: User, customer_id: int) -> list[dict[str, Any]]:
    if actor.role not in _COMPANY_CHAT_ROLES:
        raise ApiException(code=ErrorCode.FORBIDDEN, status_code=http_status.HTTP_403_FORBIDDEN)
    _require_customer_for_actor(actor, customer_id)
    messages = (
        ChatMessage.objects.select_related("sender")
        .filter(customer_id=customer_id)
        .order_by("id")[:500]
    )
    return [serialize_chat_message(m) for m in messages]


@transaction.atomic
def send_customer_chat_message(*, actor: User, customer_id: int, text: str) -> dict[str, Any]:
    if actor.role not in _COMPANY_CHAT_ROLES:
        raise ApiException(code=ErrorCode.FORBIDDEN, status_code=http_status.HTTP_403_FORBIDDEN)
    customer = _require_customer_for_actor(actor, customer_id)
    clean = _validate_message_text(text)

    prev = ChatMessage.objects.filter(customer=customer).order_by("-id").first()
    msg = ChatMessage.objects.create(
        customer=customer,
        sender=actor,
        sender_kind=ChatSenderKind.STAFF,
        text=clean,
    )
    ChatReadCursor.objects.update_or_create(
        user=actor,
        customer=customer,
        defaults={"last_read_message": msg},
    )
    if prev and prev.sender_kind == ChatSenderKind.SHOP:
        notify_customer(
            customer_id=customer.id,
            kind="support_reply",
            title_ar=f"ردّ الدعم: {actor.name}",
            title_en=f"Support replied: {actor.name}",
            body_ar=clean[:120],
            body_en=clean[:120],
            ref_type="chat",
            ref_id=str(customer.id),
        )
    record_audit(
        actor=actor,
        action="create",
        entity_type="chat",
        entity_id=msg.id,
        summary=f"Support reply to {customer.name}",
        summary_ar=f"رد دعم إلى {customer.name}",
    )
    publish_chat_message(customer.id)
    return serialize_chat_message(msg)


def mark_chat_read(*, actor: User, customer_id: int) -> dict[str, bool]:
    customer = _require_customer_for_actor(actor, customer_id)
    latest = ChatMessage.objects.filter(customer=customer).order_by("-id").first()
    ChatReadCursor.objects.update_or_create(
        user=actor,
        customer=customer,
        defaults={"last_read_message": latest},
    )
    return {"ok": True}
