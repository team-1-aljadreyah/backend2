"""Common infrastructure models (IdempotencyKey & StorageObject)."""
from __future__ import annotations

from django.conf import settings
from django.db import models


class IdempotencyKey(models.Model):
    """Stores completed request/response snapshots keyed by (scope, key) to prevent duplicate retries."""

    scope = models.CharField(max_length=120, db_index=True)
    key = models.CharField(max_length=128)
    request_hash = models.CharField(max_length=64)
    response_status = models.PositiveSmallIntegerField(default=200)
    response_body = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "common_idempotency_key"
        constraints = [
            models.UniqueConstraint(fields=["scope", "key"], name="uniq_idempotency_scope_key"),
        ]


class StorageObject(models.Model):
    """Metadata index for validated binary uploads stored in Supabase Storage or local media."""

    bucket = models.CharField(max_length=64)
    object_path = models.CharField(max_length=500, unique=True)
    mime_type = models.CharField(max_length=80, default="application/octet-stream")
    byte_size = models.PositiveBigIntegerField(default=0)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="uploaded_objects"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "common_storage_object"


class DemoSeedRecord(models.Model):
    """Tracks records created/owned by `seed_demo` for idempotent reruns and surgical cleanup."""

    seed_key = models.CharField(max_length=160, unique=True)
    profile = models.CharField(max_length=32, default="full", db_index=True)
    app_label = models.CharField(max_length=64)
    model_name = models.CharField(max_length=64)
    object_id = models.BigIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "common_demo_seed_record"
        ordering = ["-id"]
        indexes = [
            models.Index(fields=["app_label", "model_name", "object_id"], name="common_demo_target_idx"),
        ]
