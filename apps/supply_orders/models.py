"""Supply Orders domain models for inbound supplier purchasing workflows."""
from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q


class SupplyStatus(models.TextChoices):
    SENT = "sent", "Sent"
    PARTIAL = "partial", "Partial Received"
    RECEIVED = "received", "Received"
    CANCELLED = "cancelled", "Cancelled"


class SupplyOrder(models.Model):
    number = models.CharField(max_length=40, unique=True, db_index=True)
    warehouse = models.ForeignKey("warehouses.Warehouse", on_delete=models.PROTECT, related_name="supply_orders")
    supplier = models.CharField(max_length=200)
    supplier_phone = models.CharField(max_length=32, blank=True, default="")
    total = models.BigIntegerField(default=0)
    status = models.CharField(
        max_length=20, choices=SupplyStatus.choices, default=SupplyStatus.SENT, db_index=True
    )
    expected_date = models.DateField(null=True, blank=True)
    note = models.TextField(blank=True, default="")
    cancel_reason = models.TextField(blank=True, default="")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="created_supply_orders"
    )
    cancelled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="cancelled_supply_orders",
    )
    cancelled_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "supply_orders_order"
        ordering = ["-id"]
        constraints = [
            models.CheckConstraint(condition=Q(total__gte=0), name="supply_order_total_non_negative"),
        ]


class SupplyOrderLine(models.Model):
    supply_order = models.ForeignKey(SupplyOrder, on_delete=models.CASCADE, related_name="lines")
    item = models.ForeignKey("catalog.Item", on_delete=models.PROTECT, related_name="supply_order_lines")
    unit = models.CharField(max_length=16, choices=[("carton", "Carton"), ("piece", "Piece")])
    qty = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    received_qty = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))
    purchase_price = models.BigIntegerField(default=0)

    class Meta:
        db_table = "supply_orders_line"
        ordering = ["id"]
        constraints = [
            models.CheckConstraint(condition=Q(qty__gte=1), name="supply_line_qty_gte_1"),
            models.CheckConstraint(condition=Q(received_qty__gte=0), name="supply_line_rec_qty_non_negative"),
            models.CheckConstraint(condition=Q(purchase_price__gte=0), name="supply_line_price_non_negative"),
        ]
