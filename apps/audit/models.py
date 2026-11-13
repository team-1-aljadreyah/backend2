"""Append-only structured audit log for all ERP domain mutations."""
from __future__ import annotations

from django.conf import settings
from django.db import models


class AuditLog(models.Model):
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="audit_entries",
    )
    actor_name = models.CharField(max_length=150, blank=True, default="")
    actor_role = models.CharField(max_length=32, blank=True, default="")
    action = models.CharField(max_length=80, db_index=True)
    entity_type = models.CharField(max_length=60, db_index=True)
    entity_id = models.CharField(max_length=64, db_index=True)
    summary = models.CharField(max_length=300, blank=True, default="")
    summary_ar = models.CharField(max_length=300, blank=True, default="")
    before = models.JSONField(null=True, blank=True)
    after = models.JSONField(null=True, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "audit_log"
        ordering = ["-id"]
        indexes = [
            models.Index(fields=["entity_type", "entity_id"]),
        ]
