"""Notification listing and per-user read tracking views (tasks moved to tasks.py)."""
from __future__ import annotations

from typing import Any

from django.db.models import Q
from drf_spectacular.utils import extend_schema
from rest_framework import status as http_status
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.views import APIView

from apps.accounts.models import User
from apps.common.exceptions import ApiException, ErrorCode
from apps.common.responses import ok_response
from apps.notifications.push import delete_push_subscription, save_push_subscription
from apps.notifications.serializers import (
    PushSubscriptionDeleteInputSerializer,
    PushSubscriptionInputSerializer,
)

from .models import Notification, NotificationRead
from .tasks import (
    process_outbox_events_task,
    reconcile_online_payments_task,
    sweep_expired_reservations_task,
    sweep_expiry_alerts_task,
)

__all__ = [
    "NotificationListView",
    "NotificationMarkAllReadView",
    "NotificationMarkReadView",
    "list_notifications_for_actor",
    "mark_all_notifications_read",
    "mark_notification_read",
    "process_outbox_events_task",
    "reconcile_online_payments_task",
    "serialize_notification",
    "sweep_expired_reservations_task",
    "sweep_expiry_alerts_task",
]


def _derive_link(ref_type: str, ref_id: str) -> str | None:
    if not ref_type:
        return None
    if ref_type == "order" and ref_id:
        return f"/orders/{ref_id}"
    if ref_type == "invoice" and ref_id:
        return f"/invoices/{ref_id}"
    if ref_type == "payment" and ref_id:
        return f"/payments/{ref_id}"
    if ref_type == "return" and ref_id:
        return f"/returns/{ref_id}"
    if ref_type == "customer" and ref_id:
        return f"/customers/{ref_id}"
    if ref_type in ("item", "batch") and ref_id:
        return f"/items/{ref_id}" if ref_type == "item" else "/batches"
    if ref_type == "commission":
        return "/commissions"
    if ref_type == "settings":
        return "/settings"
    if ref_type == "chat":
        return "/support"
    return None


def _visible_notifications_qs(actor: User):
    return Notification.objects.filter(Q(user_id=actor.id) | (Q(user__isnull=True) & Q(target_role=actor.role))).order_by("-id")


def serialize_notification(n: Notification, *, is_read: bool) -> dict[str, Any]:
    return {
        "id": n.id,
        "type": n.kind,
        "title": n.title_ar,
        "title_en": n.title_en,
        "body": n.body_ar,
        "body_en": n.body_en,
        "link": _derive_link(n.ref_type, n.ref_id),
        "created_at": n.created_at.isoformat(),
        "read": is_read,
    }


def list_notifications_for_actor(*, actor: User) -> list[dict[str, Any]]:
    items = list(_visible_notifications_qs(actor)[:100])
    read_ids = set(
        NotificationRead.objects.filter(user=actor, notification_id__in=[n.id for n in items]).values_list("notification_id", flat=True)
    )
    return [serialize_notification(n, is_read=(n.id in read_ids)) for n in items]


def mark_notification_read(*, actor: User, notification_id: int) -> dict[str, bool]:
    n = Notification.objects.filter(id=notification_id).first()
    if not n:
        raise ApiException(code=ErrorCode.NOT_FOUND, status_code=http_status.HTTP_404_NOT_FOUND)
    if not (n.user_id == actor.id or (n.user_id is None and n.target_role == actor.role)):
        raise ApiException(code=ErrorCode.FORBIDDEN, status_code=http_status.HTTP_403_FORBIDDEN)
    NotificationRead.objects.get_or_create(notification=n, user=actor)
    return {"ok": True}


def mark_all_notifications_read(*, actor: User) -> dict[str, bool]:
    ids = list(_visible_notifications_qs(actor).values_list("id", flat=True)[:500])
    already = set(NotificationRead.objects.filter(user=actor, notification_id__in=ids).values_list("notification_id", flat=True))
    to_create = [NotificationRead(notification_id=nid, user=actor) for nid in ids if nid not in already]
    if to_create:
        NotificationRead.objects.bulk_create(to_create, ignore_conflicts=True)
    return {"ok": True}


class NotificationListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request: Request):
        return ok_response(data=list_notifications_for_actor(actor=request.user))


class NotificationMarkReadView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request: Request, notification_id: int):
        return ok_response(data=mark_notification_read(actor=request.user, notification_id=notification_id))


class NotificationMarkAllReadView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request: Request):
        return ok_response(data=mark_all_notifications_read(actor=request.user))


class PushSubscriptionCreateView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="Register device Web Push subscription",
        request=PushSubscriptionInputSerializer,
        responses={200: None},
    )
    def post(self, request: Request):
        save_push_subscription(user=request.user, data=request.data or {})
        return ok_response(data=None)


class PushSubscriptionDeleteView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="Remove device Web Push subscription",
        request=PushSubscriptionDeleteInputSerializer,
        responses={200: None},
    )
    def post(self, request: Request):
        endpoint = request.data.get("endpoint") if isinstance(request.data, dict) else ""
        delete_push_subscription(user=request.user, endpoint=endpoint)
        return ok_response(data=None)

