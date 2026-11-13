"""Web Push notification dispatch and subscription management."""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from django.conf import settings

from apps.accounts.models import Role, User
from apps.common.exceptions import ApiException, ErrorCode
from apps.notifications.models import PushSubscription

logger = logging.getLogger(__name__)


def save_push_subscription(*, user: User, data: dict[str, Any]) -> None:
    if not isinstance(data, dict):
        raise ApiException(code=ErrorCode.VALIDATION, message=("بيانات غير صالحة", "Invalid payload"))
    endpoint = str(data.get("endpoint") or "").strip()
    keys = data.get("keys") if isinstance(data.get("keys"), dict) else {}
    p256dh = str(keys.get("p256dh") or "").strip()
    auth = str(keys.get("auth") or "").strip()

    if not re.match(r"^https://", endpoint) or not p256dh or not auth:
        raise ApiException(
            code=ErrorCode.VALIDATION,
            message=("اشتراك غير صحيح", "Wrong subscription"),
            errors={"endpoint": ("اشتراك غير صحيح", "Wrong subscription")},
        )

    # Replace existing subscription for this endpoint
    PushSubscription.objects.update_or_create(
        user=user,
        endpoint=endpoint,
        defaults={"p256dh": p256dh, "auth": auth},
    )


def delete_push_subscription(*, user: User, endpoint: str) -> None:
    endpoint_clean = str(endpoint or "").strip()
    if endpoint_clean:
        PushSubscription.objects.filter(user=user, endpoint=endpoint_clean).delete()


def dispatch_push_to_user(user: User, *, title: str, body: str, url: str = "", tag: str = "") -> None:
    """Send web push notification to all subscriptions of a specific user. Prunes expired ones (404/410)."""
    subs = list(PushSubscription.objects.filter(user=user))
    if not subs:
        return

    vapid_private_key = getattr(settings, "VAPID_PRIVATE_KEY", None)
    vapid_claims = getattr(settings, "VAPID_CLAIMS", {"sub": "mailto:admin@alnahrain.iq"})
    if not vapid_private_key:
        logger.debug("VAPID_PRIVATE_KEY not configured; skipping web push delivery.")
        return

    try:
        from pywebpush import WebPushException, webpush
    except ImportError:
        logger.warning("pywebpush not installed; skipping push notification.")
        return

    payload = json.dumps({
        "title": title,
        "body": body,
        "url": url,
        "tag": tag or "general",
    })

    for sub in subs:
        subscription_info = {
            "endpoint": sub.endpoint,
            "keys": {
                "p256dh": sub.p256dh,
                "auth": sub.auth,
            },
        }
        try:
            webpush(
                subscription_info=subscription_info,
                data=payload,
                vapid_private_key=vapid_private_key,
                vapid_claims=vapid_claims,
                timeout=5,
            )
        except WebPushException as exc:
            status_code = getattr(exc.response, "status_code", None) if exc.response is not None else None
            # Drop subscription if endpoint answered 404 or 410 Gone
            if status_code in (404, 410):
                logger.info(f"Push subscription expired ({status_code}); deleting {sub.endpoint}")
                sub.delete()
            else:
                logger.warning(f"Web push dispatch failed for {sub.endpoint}: {exc}")
        except Exception as exc:
            logger.warning(f"Unexpected error in web push to {sub.endpoint}: {exc}")


def dispatch_push_to_role(role: Role | str, *, title: str, body: str, url: str = "", tag: str = "") -> None:
    """Send web push notification to all users matching target role."""
    users = User.objects.filter(role=role, active=True)
    for u in users:
        dispatch_push_to_user(u, title=title, body=body, url=url, tag=tag)
