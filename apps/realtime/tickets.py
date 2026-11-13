"""Single-use SHA-256-hashed WebSocket handshake ticket issuance and atomic redemption."""
from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta

from django.conf import settings
from django.db import transaction

from apps.accounts.models import User, UserSession
from apps.realtime.models import RealtimeTicket


def hash_ticket(raw_ticket: str) -> str:
    return hashlib.sha256(raw_ticket.encode("utf-8")).hexdigest()


def issue_realtime_ticket(
    *,
    user: User,
    session_id: str = "",
    active_customer_id: int | None = None,
    ip_address: str | None = None,
) -> tuple[str, int]:
    """Mint a short-lived opaque ticket and persist only its SHA-256 digest."""
    ttl = max(5, int(getattr(settings, "REALTIME_TICKET_TTL_SECONDS", 60)))
    raw = secrets.token_urlsafe(32)
    now = datetime.now(tz=UTC)
    RealtimeTicket.objects.create(
        token_hash=hash_ticket(raw),
        user=user,
        session_id=(session_id or "")[:64],
        active_customer_id=active_customer_id,
        expires_at=now + timedelta(seconds=ttl),
        ip_address=ip_address,
    )
    return raw, ttl


@transaction.atomic
def consume_realtime_ticket(raw_ticket: str) -> RealtimeTicket | None:
    """Atomically redeem a single-use ticket, checking expiry, user activity, and session revocation."""
    token = (raw_ticket or "").strip()
    if not token or len(token) > 256:
        return None
    digest = hash_ticket(token)
    now = datetime.now(tz=UTC)
    updated = RealtimeTicket.objects.filter(
        token_hash=digest,
        used_at__isnull=True,
        expires_at__gt=now,
    ).update(used_at=now)
    if updated != 1:
        return None

    ticket = RealtimeTicket.objects.select_related("user").filter(token_hash=digest).first()
    if ticket is None or not ticket.user.active:
        return None

    if ticket.session_id:
        sess = UserSession.objects.filter(
            session_id=ticket.session_id,
            user_id=ticket.user_id,
            revoked=False,
            revoked_at__isnull=True,
        ).first()
        if sess is None:
            return None
        if sess.active_customer_id is not None:
            ticket.active_customer_id = sess.active_customer_id

    return ticket
