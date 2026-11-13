"""Realtime WebSocket ticket authentication model."""
from __future__ import annotations

from django.conf import settings
from django.db import models


class RealtimeTicket(models.Model):
    """Short-lived, single-use SHA-256-hashed WebSocket handshake ticket."""

    token_hash = models.CharField(max_length=64, unique=True, db_index=True)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="realtime_tickets")
    session_id = models.CharField(max_length=64, blank=True, default="")
    active_customer_id = models.BigIntegerField(null=True, blank=True)
    expires_at = models.DateTimeField(db_index=True)
    used_at = models.DateTimeField(null=True, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "realtime_ticket"
        ordering = ["-id"]
