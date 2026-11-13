"""Supplier goods receipt models."""
from __future__ import annotations

from django.conf import settings
from django.db import models
from django.db.models import Q


class UnitChoice(models.TextChoices):
    CARTON = "carton", "Carton"
    PIECE = "piece", "Piece"


class Receipt(models.Model):
    number = models.CharField(max_length=40, unique=True, db_index=True)
    warehouse = models.ForeignKey("warehouses.Warehouse", on_delete=models.PROTECT, related_name="receipts")
    supply_order = models.ForeignKey(
        "supply_orders.SupplyOrder",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="receipts",
    )
    supplier = models.CharField(max_length=200, blank=True, default="")
    total_cost = models.BigIntegerField(default=0)
    client_ref = models.CharField(max_length=120, null=True, blank=True, unique=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_receipts",
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "receipts_receipt"
        ordering = ["-id"]
        constraints = [
            models.CheckConstraint(condition=Q(total_cost__gte=0), name="receipt_total_cost_non_negative"),
        ]


class ReceiptLine(models.Model):
    receipt = models.ForeignKey(Receipt, on_delete=models.CASCADE, related_name="lines")
    item = models.ForeignKey("catalog.Item", on_delete=models.PROTECT, related_name="receipt_lines")
    unit = models.CharField(max_length=16, choices=UnitChoice.choices)
    qty = models.PositiveIntegerField()
    qty_pieces = models.PositiveIntegerField()
    purchase_price = models.BigIntegerField()  # IQD per piece
    expiry_date = models.DateField()

    class Meta:
        db_table = "receipts_line"
        ordering = ["id"]
        constraints = [
            models.CheckConstraint(condition=Q(purchase_price__gte=0), name="receipt_line_price_non_negative"),
        ]
