"""ClosedCommissionMonth immutable monthly commission snapshot model."""
from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from django.db import models


class ClosedCommissionMonth(models.Model):
    month = models.CharField(max_length=7, unique=True, db_index=True)  # YYYY-MM
    pct = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("2.00"))
    snapshot = models.JSONField(default=list)
    total_collected = models.BigIntegerField(default=0)
    total_commission = models.BigIntegerField(default=0)
    closed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="closed_commission_months"
    )
    closed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "commissions_closed_month"
        ordering = ["-month"]
