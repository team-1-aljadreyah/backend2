"""Support chat messages and per-user read cursors."""
from __future__ import annotations

from django.conf import settings
from django.db import models


class ChatSenderKind(models.TextChoices):
    SHOP = "shop", "Shop"
    STAFF = "staff", "Staff"


class ChatMessage(models.Model):
    customer = models.ForeignKey(
        "customers.Customer",
        on_delete=models.CASCADE,
        related_name="chat_messages",
    )
    sender = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="chat_messages",
    )
    sender_kind = models.CharField(max_length=10, choices=ChatSenderKind.choices)
    text = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "chat_message"
        ordering = ["id"]
        indexes = [
            models.Index(fields=["customer", "id"], name="chat_msg_customer_id_idx"),
        ]

    def __str__(self) -> str:
        return f"Chat#{self.id} C#{self.customer_id} ({self.sender_kind})"


class ChatReadCursor(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="chat_read_cursors",
    )
    customer = models.ForeignKey(
        "customers.Customer",
        on_delete=models.CASCADE,
        related_name="chat_read_cursors",
    )
    last_read_message = models.ForeignKey(
        ChatMessage,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "chat_read_cursor"
        constraints = [
            models.UniqueConstraint(fields=["user", "customer"], name="uniq_chat_read_cursor"),
        ]
