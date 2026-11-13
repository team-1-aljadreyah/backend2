"""Post-commit durable Transactional Outbox & real-time WebSocket fanout helpers."""
from __future__ import annotations

import logging
import os
from datetime import UTC, datetime, timedelta
from typing import Any

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.db import transaction
from django.db.models import Q

logger = logging.getLogger("al_nahrain.realtime")

OUTBOX_MAX_ATTEMPTS = 5
OUTBOX_LEASE_SECONDS = 60


def _now_iso() -> str:
    return datetime.now(tz=UTC).isoformat()


def send_to_groups(groups: list[str] | set[str], payload: dict[str, Any], *, raise_on_error: bool = False) -> None:
    """Deliver a realtime event payload to channel layer groups."""
    if not groups:
        return
    layer = get_channel_layer()
    if layer is None:
        return
    for group in groups:
        try:
            async_to_sync(layer.group_send)(group, {"type": "rt.event", "payload": payload})
        except Exception as exc:
            logger.warning("Realtime group_send failed for group=%s: %s", group, exc)
            if raise_on_error:
                raise


def _order_groups(order_id: int) -> tuple[set[str], int] | tuple[None, None]:
    from apps.orders.models import Order

    order = (
        Order.objects.select_related("customer", "delivery")
        .prefetch_related("lines", "prep_parts")
        .filter(id=order_id)
        .first()
    )
    if not order:
        return None, None
    groups: set[str] = {
        "role_manager",
        "role_accountant",
        f"shop_{order.customer_id}",
    }
    if order.warehouse_id:
        groups.add(f"wh_{order.warehouse_id}")
    for ln in order.lines.all():
        groups.add(f"wh_{ln.warehouse_id}")
    for pp in order.prep_parts.all():
        groups.add(f"wh_{pp.warehouse_id}")
    if order.rep_id:
        groups.add(f"user_{order.rep_id}")
    if order.customer and order.customer.rep_id:
        groups.add(f"user_{order.customer.rep_id}")
    dlv = getattr(order, "delivery", None)
    if dlv and dlv.rep_id:
        groups.add(f"user_{dlv.rep_id}")
    return groups, order.id


def location_groups_for_rep(rep_id: int) -> set[str]:
    """Compute authorized WebSocket groups allowed to receive a rep's GPS updates."""
    from apps.deliveries.models import Delivery, DeliveryStatus

    groups: set[str] = {"loc_staff", f"user_{rep_id}"}
    shop_cids = (
        Delivery.objects.filter(status=DeliveryStatus.OUT)
        .filter(Q(rep_id=rep_id) | Q(rep__isnull=True, order__rep_id=rep_id))
        .values_list("order__customer_id", flat=True)
        .distinct()
    )
    for cid in shop_cids:
        groups.add(f"shop_{cid}")
    return groups


def dispatch_outbox_realtime(topic: str, payload: dict[str, Any], *, raise_on_error: bool = True) -> None:
    """Resolve authorized recipients and fan out a durable OutboxEvent over Channels."""
    norm = topic.removeprefix("rt.")
    if norm == "chat" and payload.get("customer_id"):
        cid = int(payload["customer_id"])
        send_to_groups(
            ["chat_staff", f"shop_{cid}"],
            {"topic": "chat", "type": "message", "data": {"customer_id": cid}, "at": _now_iso()},
            raise_on_error=raise_on_error,
        )
    elif norm == "orders" and payload.get("id"):
        groups, oid = _order_groups(int(payload["id"]))
        if groups and oid:
            send_to_groups(
                groups,
                {"topic": "orders", "type": "changed", "data": {"id": oid}, "at": _now_iso()},
                raise_on_error=raise_on_error,
            )
    elif norm == "location" and payload.get("rep_id"):
        rep_id = int(payload["rep_id"])
        groups = location_groups_for_rep(rep_id)
        send_to_groups(
            groups,
            {"topic": "location", "type": "moved", "data": payload, "at": str(payload.get("at") or _now_iso())},
            raise_on_error=raise_on_error,
        )
    elif norm == "notifications":
        groups_list: list[str] = []
        if payload.get("user_id"):
            groups_list.append(f"user_{int(payload['user_id'])}")
        elif payload.get("target_role"):
            groups_list.append(f"role_{payload['target_role']}")
        notif_data = {"id": int(payload["id"])} if payload.get("id") is not None else payload
        send_to_groups(
            groups_list,
            {"topic": "notifications", "type": "created", "data": notif_data, "at": _now_iso()},
            raise_on_error=raise_on_error,
        )


def deliver_outbox_event(event_id: int, *, worker_id: str = "wake") -> str:
    """
    Atomically claim a single OutboxEvent, attempt delivery, and record SENT or retry backoff.
    Returns one of: 'skipped', 'sent', 'retry', 'failed'.
    """
    from apps.notifications.models import OutboxEvent, OutboxStatus

    now = datetime.now(tz=UTC)
    stale_cutoff = now - timedelta(seconds=OUTBOX_LEASE_SECONDS)
    claimed = (
        OutboxEvent.objects.filter(id=event_id)
        .filter(
            Q(status=OutboxStatus.PENDING)
            & (Q(next_retry_at__isnull=True) | Q(next_retry_at__lte=now))
            | Q(status=OutboxStatus.PROCESSING, locked_at__lt=stale_cutoff)
        )
        .update(
            status=OutboxStatus.PROCESSING,
            locked_at=now,
            locked_by=worker_id[:64],
        )
    )
    if not claimed:
        return "skipped"

    ev = OutboxEvent.objects.filter(id=event_id).first()
    if not ev:
        return "skipped"

    try:
        dispatch_outbox_realtime(
            ev.topic,
            ev.payload if isinstance(ev.payload, dict) else {},
            raise_on_error=True,
        )
    except Exception as exc:
        attempts = int(ev.attempts) + 1
        permanently_failed = attempts >= OUTBOX_MAX_ATTEMPTS
        backoff_sec = min(300, 15 * (2 ** max(0, attempts - 1)))
        OutboxEvent.objects.filter(id=ev.id).update(
            attempts=attempts,
            last_error=str(exc)[:500],
            status=OutboxStatus.FAILED if permanently_failed else OutboxStatus.PENDING,
            next_retry_at=None if permanently_failed else now + timedelta(seconds=backoff_sec),
            locked_at=None,
            locked_by="",
        )
        return "failed" if permanently_failed else "retry"

    OutboxEvent.objects.filter(id=ev.id).update(
        attempts=int(ev.attempts) + 1,
        status=OutboxStatus.SENT,
        processed_at=now,
        next_retry_at=None,
        locked_at=None,
        locked_by="",
        last_error="",
    )
    return "sent"


def _wake_outbox_event(event_id: int) -> None:
    try:
        deliver_outbox_event(event_id, worker_id=f"commit-{os.getpid()}")
    except Exception as exc:
        logger.warning("Outbox post-commit wake failed for id=%s: %s", event_id, exc)


def enqueue_outbox_event(topic: str, payload: dict[str, Any]) -> int:
    """Persist an OutboxEvent inside the current DB transaction and trigger post-commit delivery."""
    from apps.notifications.models import OutboxEvent, OutboxStatus

    ev = OutboxEvent.objects.create(
        topic=topic,
        payload=payload,
        status=OutboxStatus.PENDING,
        next_retry_at=None,
    )
    ev_id = ev.id
    transaction.on_commit(lambda: _wake_outbox_event(ev_id))
    return ev_id


def publish_notification(notification: Any) -> int:
    """Queue and push a committed Notification event to its user or role channel."""
    return enqueue_outbox_event(
        "rt.notifications",
        {
            "id": notification.id,
            "user_id": notification.user_id,
            "target_role": notification.target_role,
        },
    )


def publish_chat_message(customer_id: int) -> int:
    """Queue and push a committed chat message wakeup to support inbox and owning shop."""
    return enqueue_outbox_event("rt.chat", {"customer_id": customer_id})


def publish_order_changed(order_id: int) -> int:
    """Queue and push a minimal order-changed wakeup strictly to authorized order observers."""
    return enqueue_outbox_event("rt.orders", {"id": order_id})


def publish_rep_location(*, rep_id: int, lat: float, lng: float, at: str) -> int:
    """Queue and push live rep coordinates after DB commit to managers, warehouse/staff, and active shops."""
    return enqueue_outbox_event("rt.location", {"rep_id": rep_id, "lat": lat, "lng": lng, "at": at})
