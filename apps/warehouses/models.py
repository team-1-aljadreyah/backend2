"""Warehouse model and operational scope helpers."""
from __future__ import annotations

from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q


class Warehouse(models.Model):
    name = models.CharField(max_length=120)
    name_en = models.CharField(max_length=120)
    code = models.CharField(max_length=32, unique=True)
    capacity = models.PositiveIntegerField(default=1000, validators=[MinValueValidator(1)])
    lat = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    lng = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    active = models.BooleanField(default=True)
    layout = models.JSONField(null=True, blank=True)
    cover = models.JSONField(null=True, blank=True)

    class Meta:
        db_table = "warehouses_warehouse"
        ordering = ["id"]
        constraints = [
            models.CheckConstraint(condition=Q(capacity__gte=1), name="warehouse_capacity_gte_1"),
        ]

    def __str__(self) -> str:
        return f"{self.name_en} ({self.code})"
