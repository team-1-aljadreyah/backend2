"""Stocktake and StocktakeLine models."""
from __future__ import annotations

from django.conf import settings
from django.db import models


class StocktakeStatus(models.TextChoices):
    OPEN = "open", "Open"
    CLOSED = "closed", "Closed"


class Stocktake(models.Model):
    number = models.CharField(max_length=40, unique=True, db_index=True)
    warehouse = models.ForeignKey("warehouses.Warehouse", on_delete=models.PROTECT, related_name="stocktakes")
    category = models.CharField(max_length=100, blank=True, default="")
    status = models.CharField(max_length=16, choices=StocktakeStatus.choices, default=StocktakeStatus.OPEN, db_index=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="created_stocktakes")
    closed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="closed_stocktakes"
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "stocktakes_stocktake"
        ordering = ["-id"]


class StocktakeLine(models.Model):
    stocktake = models.ForeignKey(Stocktake, on_delete=models.CASCADE, related_name="lines")
    item = models.ForeignKey("catalog.Item", on_delete=models.PROTECT)
    system_qty = models.PositiveIntegerField()
    counted_qty = models.IntegerField(null=True, blank=True)
    damaged_qty = models.PositiveIntegerField(default=0)
    reason = models.TextField(blank=True, default="")

    class Meta:
        db_table = "stocktakes_line"
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(fields=["stocktake", "item"], name="uniq_stocktake_item"),
        ]
