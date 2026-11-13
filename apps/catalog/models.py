"""Catalog models: Category, Item, ItemPrice, PriceChange."""
from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone


class Category(models.Model):
    name = models.CharField(max_length=100, unique=True)
    name_en = models.CharField(max_length=100, blank=True, default="")
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = "catalog_category"
        ordering = ["sort_order", "id"]

    def __str__(self) -> str:
        return self.name


class Item(models.Model):
    name = models.CharField(max_length=200)
    name_en = models.CharField(max_length=200)
    barcode = models.CharField(max_length=64, blank=True, default="", db_index=True)
    category = models.CharField(max_length=100, db_index=True)
    pieces_per_carton = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    min_stock = models.PositiveIntegerField(default=0)
    avg_cost = models.BigIntegerField(default=0)
    commission_percent = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0.00"))
    weight_kg = models.DecimalField(max_digits=8, decimal_places=3, null=True, blank=True)
    brand = models.CharField(max_length=40, blank=True, default="")
    video = models.CharField(max_length=500, blank=True, default="")
    pack_name = models.CharField(max_length=20, blank=True, default="")
    piece_name = models.CharField(max_length=20, blank=True, default="")
    sells = models.CharField(max_length=10, blank=True, default="both")
    offer = models.JSONField(null=True, blank=True)
    image_path = models.CharField(max_length=255, blank=True, default="")
    active = models.BooleanField(default=True, db_index=True)
    deleted = models.BooleanField(default=False, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "catalog_item"
        ordering = ["id"]
        constraints = [
            models.CheckConstraint(condition=Q(pieces_per_carton__gte=1), name="item_pieces_per_carton_gte_1"),
            models.CheckConstraint(condition=Q(avg_cost__gte=0), name="item_avg_cost_non_negative"),
            models.CheckConstraint(
                condition=Q(commission_percent__gte=0, commission_percent__lte=100),
                name="item_commission_percent_bounds",
            ),
            models.CheckConstraint(
                condition=Q(weight_kg__isnull=True) | Q(weight_kg__gte=Decimal("0.01"), weight_kg__lte=Decimal("100.000")),
                name="item_weight_kg_bounds",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.name_en} (#{self.id})"


class ItemPrice(models.Model):
    item = models.ForeignKey(Item, on_delete=models.CASCADE, related_name="prices")
    tier = models.ForeignKey("accounts.PriceTier", on_delete=models.CASCADE, related_name="item_prices")
    carton_price = models.BigIntegerField(default=0)
    piece_price = models.BigIntegerField(default=0)

    class Meta:
        db_table = "catalog_item_price"
        ordering = ["tier_id"]
        constraints = [
            models.UniqueConstraint(fields=["item", "tier"], name="uniq_item_price_per_tier"),
            models.CheckConstraint(condition=Q(carton_price__gte=0), name="item_price_carton_non_negative"),
            models.CheckConstraint(condition=Q(piece_price__gte=0), name="item_price_piece_non_negative"),
        ]


class PriceChange(models.Model):
    """Timestamped tier price history enabling 3-day offline pricing resolution."""

    item = models.ForeignKey(Item, on_delete=models.CASCADE, related_name="price_changes")
    tier = models.ForeignKey("accounts.PriceTier", on_delete=models.CASCADE, related_name="price_changes")
    carton_price = models.BigIntegerField()
    piece_price = models.BigIntegerField()
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    effective_from = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        db_table = "catalog_price_change"
        ordering = ["-effective_from", "-id"]
        indexes = [
            models.Index(fields=["item", "tier", "effective_from"]),
        ]
