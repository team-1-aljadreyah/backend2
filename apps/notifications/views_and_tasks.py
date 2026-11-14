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
from apps.notifications.services import derive_notification_link

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


def _visible_notifications_qs(actor: User):
    return Notification.objects.filter(Q(user_id=actor.id) | (Q(user__isnull=True) & Q(target_role=actor.role))).order_by("-id")


def serialize_notification(n: Notification, *, is_read: bool, actor: User | None = None) -> dict[str, Any]:
    link_actor = actor if actor is not None else getattr(n, "user", None)
    out: dict[str, Any] = {
        "id": n.id,
        "type": n.kind,
        "title": n.title_ar,
        "title_en": n.title_en,
        "body": n.body_ar,
        "body_en": n.body_en,
        "link": derive_notification_link(
            n.ref_type,
            n.ref_id,
            actor=link_actor,
            fallback_role=n.target_role,
        ),
        "created_at": n.created_at.isoformat(),
        "read": is_read,
        "roles": [n.target_role] if n.target_role else [],
    }
    if n.user_id is not None:
        out["user_id"] = n.user_id
    return out


def list_notifications_for_actor(*, actor: User) -> list[dict[str, Any]]:
    items = list(_visible_notifications_qs(actor).select_related("user")[:100])
    read_ids = set(
        NotificationRead.objects.filter(user=actor, notification_id__in=[n.id for n in items]).values_list("notification_id", flat=True)
    )
    return [serialize_notification(n, is_read=(n.id in read_ids), actor=actor) for n in items]


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

