"""Payment, PaymentAllocation, Refund, and OnlinePayment models."""
from __future__ import annotations

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q


class PaymentMethod(models.TextChoices):
    CASH = "cash", "Cash"
    TRANSFER = "transfer", "Transfer"
    ONLINE = "online", "Online (Qi Card)"


class OnlinePaymentStatus(models.TextChoices):
    CREATED = "created", "Created"
    SUCCESS = "success", "Success"
    FAILED = "failed", "Failed"
    CANCELLED = "cancelled", "Cancelled"
    EXPIRED = "expired", "Expired"


class Payment(models.Model):
    number = models.CharField(max_length=40, unique=True, db_index=True)
    customer = models.ForeignKey("customers.Customer", on_delete=models.PROTECT, related_name="payments")
    received_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="received_payments",
    )
    received_by_name = models.CharField(max_length=150, blank=True, default="")
    received_by_role = models.CharField(max_length=24, blank=True, default="shop")
    method = models.CharField(max_length=16, choices=PaymentMethod.choices, default=PaymentMethod.CASH)
    amount = models.BigIntegerField()  # Positive for collections, negative equal counter-value for reversals
    unallocated_amount = models.BigIntegerField(default=0)
    note = models.TextField(blank=True, default="")
    reason = models.TextField(blank=True, default="")

    reversed = models.BooleanField(default=False, db_index=True)
    reversal_of = models.OneToOneField(
        "self", on_delete=models.PROTECT, null=True, blank=True, related_name="reversed_by_entry"
    )
    reversed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="payment_reversals",
    )
    reversed_at = models.DateTimeField(null=True, blank=True)
    gateway = models.JSONField(null=True, blank=True)
    client_ref = models.CharField(max_length=120, unique=True, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "payments_payment"
        ordering = ["-id"]
        constraints = [
            models.CheckConstraint(condition=~Q(amount=0), name="payment_amount_non_zero"),
        ]


class PaymentAllocation(models.Model):
    payment = models.ForeignKey(Payment, on_delete=models.CASCADE, related_name="allocations")
    invoice = models.ForeignKey("invoices.Invoice", on_delete=models.PROTECT, related_name="payment_allocations")
    amount = models.BigIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "payments_allocation"
        ordering = ["id"]


class Refund(models.Model):
    number = models.CharField(max_length=40, unique=True, db_index=True)
    customer = models.ForeignKey("customers.Customer", on_delete=models.PROTECT, related_name="refunds")
    payment = models.ForeignKey(
        Payment, on_delete=models.SET_NULL, null=True, blank=True, related_name="refunds"
    )
    amount = models.BigIntegerField(validators=[MinValueValidator(1)])
    method = models.CharField(max_length=16, choices=PaymentMethod.choices, default=PaymentMethod.CASH)
    reason = models.TextField()
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="created_refunds"
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "payments_refund"
        ordering = ["-id"]
        constraints = [
            models.CheckConstraint(condition=Q(amount__gt=0), name="refund_amount_gt_zero"),
        ]


class OnlinePayment(models.Model):
    request_id = models.CharField(max_length=64, unique=True, db_index=True)
    payment_id = models.CharField(max_length=120, unique=True, db_index=True)
    provider = models.CharField(max_length=20, default="qi", db_index=True)
    gateway_id = models.CharField(max_length=120, blank=True, default="", db_index=True)
    customer = models.ForeignKey("customers.Customer", on_delete=models.PROTECT, related_name="online_payments")
    initiated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="online_payments"
    )
    amount = models.BigIntegerField(validators=[MinValueValidator(1)])
    status = models.CharField(
        max_length=20,
        choices=OnlinePaymentStatus.choices,
        default=OnlinePaymentStatus.CREATED,
        db_index=True,
    )
    form_url = models.TextField(blank=True, default="")
    return_url = models.TextField(blank=True, default="")
    note = models.CharField(max_length=255, blank=True, default="")
    client_ref = models.CharField(max_length=120, unique=True, null=True, blank=True)
    recorded_payment = models.OneToOneField(
        Payment, on_delete=models.SET_NULL, null=True, blank=True, related_name="online_payment"
    )
    raw_payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "payments_online"
        ordering = ["-id"]
        constraints = [
            models.CheckConstraint(condition=Q(amount__gt=0), name="online_payment_amount_gt_zero"),
        ]


class WebhookEvent(models.Model):
    """Stores verified payment webhook events for audit & duplicate replay protection."""

    provider = models.CharField(max_length=40, default="qi_card")
    event_id = models.CharField(max_length=120)
    signature_valid = models.BooleanField(default=False)
    payload = models.JSONField(default=dict, blank=True)
    processed = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "payments_webhook_event"
        constraints = [
            models.UniqueConstraint(fields=["provider", "event_id"], name="uniq_payment_webhook_provider_event"),
        ]
