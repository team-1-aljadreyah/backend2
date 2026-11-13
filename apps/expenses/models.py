"""Expense tracking models with optional approval threshold and cancellation."""
from __future__ import annotations

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q


class ExpenseStatus(models.TextChoices):
    APPROVED = "approved", "Approved"
    PENDING = "pending", "Pending Manager Approval"
    REJECTED = "rejected", "Rejected"
    CANCELLED = "cancelled", "Cancelled"


class ExpenseMethod(models.TextChoices):
    CASH = "cash", "Cash"
    TRANSFER = "transfer", "Transfer"


class Expense(models.Model):
    """Operating expense ledger deducted from net monthly profit."""

    number = models.CharField(max_length=40, unique=True, db_index=True)
    date = models.DateField(db_index=True)
    category = models.CharField(max_length=40, db_index=True)
    amount = models.BigIntegerField(validators=[MinValueValidator(1)])
    method = models.CharField(max_length=16, choices=ExpenseMethod.choices, default=ExpenseMethod.CASH)
    paid_to = models.CharField(max_length=180)
    note = models.TextField(blank=True, default="")
    warehouse = models.ForeignKey(
        "warehouses.Warehouse", on_delete=models.SET_NULL, null=True, blank=True, related_name="expenses"
    )
    vehicle = models.ForeignKey(
        "deliveries.Vehicle", on_delete=models.SET_NULL, null=True, blank=True, related_name="expenses"
    )
    photo_path = models.CharField(max_length=255, blank=True, default="")
    status = models.CharField(
        max_length=20, choices=ExpenseStatus.choices, default=ExpenseStatus.APPROVED, db_index=True
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="created_expenses"
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="decided_expenses"
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    reject_reason = models.TextField(blank=True, default="")
    cancelled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="cancelled_expenses"
    )
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancel_reason = models.TextField(blank=True, default="")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "expenses_expense"
        ordering = ["-id"]
        constraints = [
            models.CheckConstraint(condition=Q(amount__gt=0), name="expense_amount_positive"),
        ]

    def __str__(self) -> str:
        return f"{self.number} ({self.amount} IQD)"