"""Notification, per-user read status, and transactional OutboxEvent models."""
from __future__ import annotations

from django.conf import settings
from django.db import models


class Notification(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="direct_notifications",
    )
    target_role = models.CharField(max_length=32, blank=True, default="", db_index=True)
    kind = models.CharField(max_length=60, db_index=True)
    title_ar = models.CharField(max_length=255)
    title_en = models.CharField(max_length=255)
    body_ar = models.TextField(blank=True, default="")
    body_en = models.TextField(blank=True, default="")
    ref_type = models.CharField(max_length=60, blank=True, default="")
    ref_id = models.CharField(max_length=64, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "notifications_notification"
        ordering = ["-id"]


class NotificationRead(models.Model):
    """Per-user read receipt so role broadcasts do not mark read for other users."""

    notification = models.ForeignKey(Notification, on_delete=models.CASCADE, related_name="reads")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notification_reads")
    read_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "notifications_read"
        constraints = [
            models.UniqueConstraint(fields=["notification", "user"], name="uniq_notification_read_per_user"),
        ]


class OutboxStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    PROCESSING = "processing", "Processing"
    SENT = "sent", "Sent"
    FAILED = "failed", "Failed"


class OutboxEvent(models.Model):
    topic = models.CharField(max_length=80, db_index=True)
    payload = models.JSONField(default=dict)
    status = models.CharField(max_length=20, choices=OutboxStatus.choices, default=OutboxStatus.PENDING, db_index=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    next_retry_at = models.DateTimeField(null=True, blank=True, db_index=True)
    locked_at = models.DateTimeField(null=True, blank=True)
    locked_by = models.CharField(max_length=64, blank=True, default="")
    last_error = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    processed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "notifications_outbox"
        ordering = ["id"]
        indexes = [
            models.Index(fields=["status", "next_retry_at"], name="notif_outbox_status_retry_idx"),
        ]


class PushSubscription(models.Model):
    """Web Push subscription endpoints for browser push notifications."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="push_subscriptions",
    )
    endpoint = models.URLField(max_length=500, db_index=True)
    p256dh = models.CharField(max_length=255)
    auth = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "notifications_push_subscription"
        ordering = ["-id"]
        constraints = [
            models.UniqueConstraint(fields=["user", "endpoint"], name="uniq_push_sub_user_endpoint"),
        ]

