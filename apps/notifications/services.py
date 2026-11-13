"""Notification & outbox emission services."""
from __future__ import annotations

from typing import Any

from apps.realtime.broadcaster import publish_notification

from .models import Notification, OutboxEvent


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
) -> Notification:
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
        from apps.accounts.models import User

        from .push import dispatch_push_to_user

        target_u = User.objects.filter(id=user_id).first()
        if target_u:
            url = f"/{ref_type}s/{ref_id}" if ref_type and ref_id else ""
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

        url = f"/{ref_type}s/{ref_id}" if ref_type and ref_id else ""
        dispatch_push_to_role(role, title=title_ar, body=body_ar or title_en, url=url, tag=kind)
    except Exception:
        pass
    return notif


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
    from apps.customers.models import ShopMembership

    user_ids = list(
        ShopMembership.objects.filter(customer_id=customer_id, user__active=True)
        .values_list("user_id", flat=True)
        .distinct()
    )
    return [
        notify_user(
            user_id=uid,
            kind=kind,
            title_ar=title_ar,
            title_en=title_en,
            body_ar=body_ar,
            body_en=body_en,
            ref_type=ref_type,
            ref_id=ref_id,
        )
        for uid in user_ids
    ]


def enqueue_outbox(topic: str, payload: dict[str, Any]) -> OutboxEvent:
    return OutboxEvent.objects.create(topic=topic, payload=payload)
