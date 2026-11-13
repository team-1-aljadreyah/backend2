"""Celery background and scheduled operational tasks."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from celery import shared_task
from django.apps import apps

from apps.accounts.models import Role, SystemSetting
from apps.common.utils import baghdad_today

from .models import Notification, OutboxEvent, OutboxStatus
from .services import notify_role


@shared_task(name="notifications.sweep_expired_reservations")
def sweep_expired_reservations_task() -> int:
    if not apps.is_installed("apps.orders"):
        return 0
    from apps.orders.services import sweep_expired_reservations

    return sweep_expired_reservations()


@shared_task(name="notifications.sweep_expiry_alerts")
def sweep_expiry_alerts_task() -> dict[str, int]:
    if not apps.is_installed("apps.inventory"):
        return {"near_expiry": 0, "expired": 0}
    from apps.inventory.models import Batch

    today = baghdad_today()
    near_days = SystemSetting.get_solo().near_expiry_days
    cutoff = today + timedelta(days=near_days)
    today_iso = today.isoformat()

    expired_count = 0
    near_count = 0
    for batch in Batch.objects.select_related("item").filter(
        qty_left__gt=0, held_at__isnull=True, expiry_date__lte=cutoff
    ):
        is_exp = batch.expiry_date < today
        kind = "expired" if is_exp else "near_expiry"
        marker = f"{batch.id}:{today_iso}"
        if Notification.objects.filter(kind=kind, ref_type="batch", ref_id=marker).exists():
            continue
        title_ar = (
            f"انتهت صلاحية دفعة #{batch.id} ({batch.item.name})"
            if is_exp
            else f"قرب انتهاء صلاحية #{batch.id} ({batch.item.name})"
        )
        title_en = (
            f"Expired batch #{batch.id} ({batch.item.name_en})"
            if is_exp
            else f"Near-expiry batch #{batch.id} ({batch.item.name_en})"
        )
        for target_role in (Role.MANAGER, Role.WAREHOUSE):
            notify_role(
                role=target_role,
                kind=kind,
                title_ar=title_ar,
                title_en=title_en,
                body_ar=f"تاريخ الانتهاء: {batch.expiry_date.isoformat()} · المتبقي: {batch.qty_left}",
                body_en=f"Expiry: {batch.expiry_date.isoformat()} · Left: {batch.qty_left}",
                ref_type="batch",
                ref_id=marker,
            )
        if is_exp:
            expired_count += 1
        else:
            near_count += 1
    return {"near_expiry": near_count, "expired": expired_count}


@shared_task(name="notifications.process_outbox_events")
def process_outbox_events_task(batch_size: int = 50, worker_id: str | None = None) -> dict[str, int]:
    from django.db.models import Q

    from apps.realtime.broadcaster import OUTBOX_LEASE_SECONDS, deliver_outbox_event

    processed = 0
    failed = 0
    requeued = 0
    now = datetime.now(tz=UTC)
    stale_cutoff = now - timedelta(seconds=OUTBOX_LEASE_SECONDS)
    wid = worker_id or f"celery-{datetime.now(tz=UTC).strftime('%H%M%S')}"

    candidate_ids = list(
        OutboxEvent.objects.filter(
            Q(status=OutboxStatus.PENDING) & (Q(next_retry_at__isnull=True) | Q(next_retry_at__lte=now))
            | Q(status=OutboxStatus.PROCESSING, locked_at__lt=stale_cutoff)
        )
        .order_by("id")
        .values_list("id", flat=True)[:batch_size]
    )
    for ev_id in candidate_ids:
        outcome = deliver_outbox_event(ev_id, worker_id=wid)
        if outcome == "sent":
            processed += 1
        elif outcome == "retry":
            requeued += 1
            failed += 1
        elif outcome == "failed":
            failed += 1
    return {"processed": processed, "failed": failed, "requeued": requeued}


@shared_task(name="realtime.cleanup_expired_tickets")
def cleanup_realtime_tickets_task() -> int:
    if not apps.is_installed("apps.realtime"):
        return 0
    from django.db.models import Q

    from apps.realtime.models import RealtimeTicket

    now = datetime.now(tz=UTC)
    exp_cutoff = now - timedelta(minutes=5)
    used_cutoff = now - timedelta(hours=1)
    deleted, _ = RealtimeTicket.objects.filter(
        Q(expires_at__lte=exp_cutoff) | Q(used_at__isnull=False, used_at__lte=used_cutoff)
    ).delete()
    return int(deleted)


@shared_task(name="payments.reconcile_online_payments")
def reconcile_online_payments_task() -> int:
    if not apps.is_installed("apps.payments"):
        return 0
    from apps.payments.models import OnlinePayment, OnlinePaymentStatus
    from apps.payments.services import settle_online_payment
    from integrations.qi_card.client import QiCardGateway

    gw = QiCardGateway()
    if not gw.is_configured():
        return 0
    updated = 0
    cutoff = datetime.now(tz=UTC) - timedelta(minutes=2)
    for op in OnlinePayment.objects.filter(
        status=OnlinePaymentStatus.CREATED, created_at__lte=cutoff
    )[:30]:
        try:
            st = gw.get_payment_status(op.payment_id)
            if st.status != op.status:
                settle_online_payment(
                    online_payment_id=op.id,
                    new_status=st.status,
                    raw_payload=st.raw,
                    masked_card=st.masked_card,
                )
                updated += 1
        except Exception:
            continue
    return updated
