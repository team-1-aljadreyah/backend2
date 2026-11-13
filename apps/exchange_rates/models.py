"""Daily IQD/USD exchange rate history model."""
from __future__ import annotations

from django.conf import settings
from django.db import models
from django.db.models import Q


class UsdRate(models.Model):
    date = models.DateField(unique=True, db_index=True)
    rate = models.DecimalField(max_digits=12, decimal_places=2)
    by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="usd_rates")
    at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "exchange_rates_usd"
        ordering = ["-date", "-at"]
        constraints = [
            models.CheckConstraint(condition=Q(rate__gt=0), name="usd_rate_gt_zero"),
        ]
