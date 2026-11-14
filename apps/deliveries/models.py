"""Delivery model tracking QR dispatch, PIN attempts/locks, private proof photo, and failures."""
from __future__ import annotations

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q


class Vehicle(models.Model):
    """Fleet vehicles with driver assignments and weight capacities for loading plans."""

    name = models.CharField(max_length=150)
    plate = models.CharField(max_length=64, blank=True, default="")
    capacity_kg = models.PositiveIntegerField(default=1000, validators=[MinValueValidator(50)])
    driver = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="vehicles",
    )
    active = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "deliveries_vehicle"
        ordering = ["id"]
        constraints = [
            models.CheckConstraint(
                condition=Q(capacity_kg__gte=50, capacity_kg__lte=40000),
                name="vehicle_capacity_kg_bounds",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.plate or self.capacity_kg})"


class DeliveryStatus(models.TextChoices):
    READY = "ready", "Ready for Dispatch"
    OUT = "out", "Out for Delivery"
    DELIVERED = "delivered", "Delivered"
    PROBLEM = "problem", "Problem Reported"
    RETURNED = "returned", "Returned to Warehouse"


class Delivery(models.Model):
    order = models.OneToOneField("orders.Order", on_delete=models.CASCADE, related_name="delivery")
    code = models.CharField(max_length=24, unique=True, db_index=True)
    pin_encrypted = models.TextField()
    pin_hash = models.CharField(max_length=128, blank=True, default="")
    pin_attempts = models.PositiveSmallIntegerField(default=0)
    locked = models.BooleanField(default=False, db_index=True)
    rep = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="assigned_deliveries"
    )
    dispatched_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="dispatched_deliveries"
    )
    delivered_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="confirmed_deliveries"
    )
    vehicle = models.ForeignKey(
        Vehicle, on_delete=models.SET_NULL, null=True, blank=True, related_name="deliveries"
    )
    loaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="loaded_deliveries"
    )
    loaded_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=DeliveryStatus.choices, default=DeliveryStatus.READY, db_index=True)
    photo_path = models.CharField(max_length=255, blank=True, default="")
    payment = models.JSONField(null=True, blank=True)
    problem_reason = models.TextField(blank=True, default="")
    problem_note = models.TextField(blank=True, default="")
    problem_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reported_delivery_problems",
    )
    problem_at = models.DateTimeField(null=True, blank=True)
    dispatched_at = models.DateTimeField(null=True, blank=True)
    out_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="out_deliveries",
    )
    out_at = models.DateTimeField(null=True, blank=True)
    out_phone = models.CharField(max_length=20, blank=True, default="")
    delivered_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "deliveries_delivery"
        ordering = ["-id"]


class RepLocation(models.Model):
    """Latest live GPS coordinates reported by active delivery reps."""

    rep = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="live_location"
    )
    lat = models.DecimalField(max_digits=9, decimal_places=6)
    lng = models.DecimalField(max_digits=9, decimal_places=6)
    updated_at = models.DateTimeField(auto_now=True, db_index=True)

    class Meta:
        db_table = "deliveries_rep_location"
        ordering = ["-updated_at"]
