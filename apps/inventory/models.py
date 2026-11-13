"""Inventory models: Batch (FEFO + holds), InventoryMovement ledger, DamagedInventory."""
from __future__ import annotations

from django.conf import settings
from django.db import models
from django.db.models import Q


class MovementKind(models.TextChoices):
    RECEIPT = "receipt", "Receipt"
    SALE = "sale", "Sale"
    TRANSFER_OUT = "transfer_out", "Transfer Out"
    TRANSFER_IN = "transfer_in", "Transfer In"
    RETURN_GOOD = "return_good", "Good Return"
    STOCKTAKE_ADJUST = "stocktake_adjust", "Stocktake Adjustment"


class DamageSource(models.TextChoices):
    RETURN = "return", "Return"
    STOCKTAKE = "stocktake", "Stocktake"
    EXPIRED = "expired", "Expired"
    DELIVERY = "delivery", "Delivery Problem"


class Batch(models.Model):
    item = models.ForeignKey("catalog.Item", on_delete=models.PROTECT, related_name="batches")
    warehouse = models.ForeignKey("warehouses.Warehouse", on_delete=models.PROTECT, related_name="batches")
    receipt = models.ForeignKey(
        "receipts.Receipt",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="batches",
    )
    receipt_line = models.OneToOneField(
        "receipts.ReceiptLine",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_batch",
    )
    origin_batch = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="derived_batches",
    )
    qty_received = models.PositiveIntegerField()
    qty_left = models.IntegerField()
    purchase_price = models.BigIntegerField()  # IQD per piece
    expiry_date = models.DateField(db_index=True)
    held_reason = models.CharField(max_length=255, blank=True, default="")
    held_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="held_batches",
    )
    held_at = models.DateTimeField(null=True, blank=True, db_index=True)
    received_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "inventory_batch"
        ordering = ["expiry_date", "id"]
        indexes = [
            models.Index(fields=["item", "warehouse", "expiry_date"]),
        ]
        constraints = [
            models.CheckConstraint(condition=Q(qty_left__gte=0), name="batch_qty_left_non_negative"),
            models.CheckConstraint(condition=Q(purchase_price__gte=0), name="batch_purchase_price_non_negative"),
        ]

    @property
    def is_held(self) -> bool:
        return self.held_at is not None


class InventoryMovement(models.Model):
    warehouse = models.ForeignKey("warehouses.Warehouse", on_delete=models.PROTECT, related_name="movements")
    item = models.ForeignKey("catalog.Item", on_delete=models.PROTECT, related_name="movements")
    batch = models.ForeignKey(Batch, on_delete=models.PROTECT, null=True, blank=True, related_name="movements")
    kind = models.CharField(max_length=24, choices=MovementKind.choices, db_index=True)
    qty_delta = models.IntegerField()
    balance_after = models.IntegerField()
    ref_type = models.CharField(max_length=40, blank=True, default="")
    ref_id = models.CharField(max_length=64, blank=True, default="")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "inventory_movement"
        ordering = ["-id"]
        indexes = [
            models.Index(fields=["ref_type", "ref_id"]),
        ]


class DamagedInventory(models.Model):
    warehouse = models.ForeignKey("warehouses.Warehouse", on_delete=models.PROTECT, related_name="damaged_records")
    item = models.ForeignKey("catalog.Item", on_delete=models.PROTECT, related_name="damaged_records")
    batch = models.ForeignKey(Batch, on_delete=models.SET_NULL, null=True, blank=True, related_name="damaged_records")
    pieces = models.PositiveIntegerField()
    source = models.CharField(max_length=20, choices=DamageSource.choices, db_index=True)
    ref = models.CharField(max_length=80)
    reason = models.TextField(blank=True, default="")
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "inventory_damaged"
        ordering = ["-id"]
