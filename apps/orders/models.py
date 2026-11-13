"""Order, OrderLine, and PrepPart models."""
from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q


class OrderStatus(models.TextChoices):
    PENDING = "pending", "Waiting Approval"
    APPROVED = "approved", "Approved"
    NEEDS_APPROVAL = "needs_approval", "Needs Manager Approval"
    CONFIRMED = "confirmed", "Delivered"
    CANCELLED = "cancelled", "Cancelled"
    EXPIRED = "expired", "Hold Expired / Waiting Stock"


class OrderSource(models.TextChoices):
    SHOP = "shop", "Shop"
    OFFICE = "office", "Office"
    REP = "rep", "Rep"
    COUNTER = "counter", "Counter"


class Order(models.Model):
    number = models.CharField(max_length=40, unique=True, db_index=True)
    customer = models.ForeignKey("customers.Customer", on_delete=models.PROTECT, related_name="orders")
    warehouse = models.ForeignKey("warehouses.Warehouse", on_delete=models.PROTECT, related_name="orders")
    rep = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="rep_orders"
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="created_orders"
    )
    source = models.CharField(max_length=16, choices=OrderSource.choices, default=OrderSource.OFFICE)
    status = models.CharField(max_length=24, choices=OrderStatus.choices, default=OrderStatus.PENDING, db_index=True)
    waiting_stock = models.BooleanField(default=False, db_index=True)
    approval_for = models.JSONField(default=list, blank=True)
    notes = models.TextField(blank=True, default="")

    subtotal = models.BigIntegerField(default=0)
    discount_percent = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0.00"))
    discount_amount = models.BigIntegerField(default=0)
    discount_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="discounted_orders"
    )
    discount_at = models.DateTimeField(null=True, blank=True)
    rounding = models.BigIntegerField(default=0)
    total = models.BigIntegerField(default=0)

    reserved_until = models.DateTimeField(null=True, blank=True, db_index=True)
    client_ref = models.CharField(max_length=120, unique=True, null=True, blank=True)
    offline = models.BooleanField(default=False)
    recorded_at = models.DateTimeField(null=True, blank=True)

    parent = models.ForeignKey("self", on_delete=models.SET_NULL, null=True, blank=True, related_name="child_orders")
    rest_order = models.ForeignKey("self", on_delete=models.SET_NULL, null=True, blank=True, related_name="origin_for_rest")
    shipment_of = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True, related_name="shipment_children"
    )
    shipment_no = models.PositiveIntegerField(null=True, blank=True)
    rule_note = models.TextField(blank=True, default="")
    counter = models.JSONField(null=True, blank=True)
    rating = models.JSONField(null=True, blank=True)
    short_lines = models.JSONField(default=list, blank=True)
    cancel_reason = models.TextField(blank=True, default="")
    cancelled_at = models.DateTimeField(null=True, blank=True)
    accepted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="accepted_orders"
    )
    accepted_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="approved_orders"
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    edited_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "orders_order"
        ordering = ["-id"]
        constraints = [
            models.CheckConstraint(condition=Q(subtotal__gte=0), name="order_subtotal_non_negative"),
            models.CheckConstraint(condition=Q(total__gte=0), name="order_total_non_negative"),
        ]


class OrderLine(models.Model):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="lines")
    item = models.ForeignKey("catalog.Item", on_delete=models.PROTECT)
    warehouse = models.ForeignKey(
        "warehouses.Warehouse", on_delete=models.PROTECT, null=True, blank=True, related_name="order_lines"
    )
    unit = models.CharField(max_length=16, choices=[("carton", "Carton"), ("piece", "Piece")])
    qty = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    pieces = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    unit_price = models.BigIntegerField()
    offer_off = models.BigIntegerField(default=0)
    clearance = models.BooleanField(default=False)
    total = models.BigIntegerField()

    class Meta:
        db_table = "orders_line"
        ordering = ["id"]


class PrepPart(models.Model):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="prep_parts")
    warehouse = models.ForeignKey("warehouses.Warehouse", on_delete=models.PROTECT)
    prepared_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True
    )
    prepared_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "orders_prep_part"
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(fields=["order", "warehouse"], name="uniq_order_prep_part"),
        ]
