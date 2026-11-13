"""Transfer models: Transfer, TransferLine, TransferBatchAllocation."""
from __future__ import annotations

from django.conf import settings
from django.db import models


class TransferStatus(models.TextChoices):
    SENT = "sent", "Sent"
    RECEIVED = "received", "Received"
    DISCREPANCY = "discrepancy", "Discrepancy"


class Transfer(models.Model):
    number = models.CharField(max_length=40, unique=True, db_index=True)
    from_warehouse = models.ForeignKey("warehouses.Warehouse", on_delete=models.PROTECT, related_name="outgoing_transfers")
    to_warehouse = models.ForeignKey("warehouses.Warehouse", on_delete=models.PROTECT, related_name="incoming_transfers")
    status = models.CharField(max_length=20, choices=TransferStatus.choices, default=TransferStatus.SENT, db_index=True)
    reason = models.TextField(blank=True, default="")
    client_ref = models.CharField(max_length=100, unique=True, null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="created_transfers")
    received_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="received_transfers"
    )
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="reviewed_transfers"
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    received_at = models.DateTimeField(null=True, blank=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "transfers_transfer"
        ordering = ["-id"]


class TransferLine(models.Model):
    transfer = models.ForeignKey(Transfer, on_delete=models.CASCADE, related_name="lines")
    item = models.ForeignKey("catalog.Item", on_delete=models.PROTECT)
    pieces = models.PositiveIntegerField()
    received_pieces = models.PositiveIntegerField(null=True, blank=True)

    class Meta:
        db_table = "transfers_line"
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(fields=["transfer", "item"], name="uniq_transfer_item"),
        ]


class TransferBatchAllocation(models.Model):
    """Preserves source batch expiry, cost, and receipt lineage across warehouse transfers."""

    transfer_line = models.ForeignKey(TransferLine, on_delete=models.CASCADE, related_name="batch_allocations")
    source_batch = models.ForeignKey("inventory.Batch", on_delete=models.PROTECT, related_name="transfer_allocations")
    qty = models.PositiveIntegerField()
    expiry_date = models.DateField()
    purchase_price = models.BigIntegerField()
    receipt_id = models.BigIntegerField(null=True, blank=True)

    class Meta:
        db_table = "transfers_batch_allocation"
        ordering = ["id"]
