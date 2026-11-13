"""Invoice, CreditNote, ReturnDoc, and batch lineage allocation models."""
from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone


class ReturnCondition(models.TextChoices):
    GOOD = "good", "Good (Back to Shelf)"
    DAMAGED = "damaged", "Damaged"


class Invoice(models.Model):
    number = models.CharField(max_length=40, unique=True, db_index=True)
    order = models.OneToOneField("orders.Order", on_delete=models.PROTECT, related_name="invoice")
    customer = models.ForeignKey("customers.Customer", on_delete=models.PROTECT, related_name="invoices")
    rep = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="sales_invoices",
    )
    delivery_rep = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="delivered_invoices",
    )
    warehouse = models.ForeignKey("warehouses.Warehouse", on_delete=models.PROTECT, related_name="invoices")

    subtotal = models.BigIntegerField(default=0)
    discount_percent = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0.00"))
    discount = models.BigIntegerField(default=0)
    rounding = models.BigIntegerField(default=0)
    total = models.BigIntegerField(default=0)
    cost = models.BigIntegerField(default=0)

    paid = models.BigIntegerField(default=0)
    returned = models.BigIntegerField(default=0)
    credited = models.BigIntegerField(default=0)
    usd_rate = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("1500.00"))
    counter = models.JSONField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_invoices",
    )

    delivered_at = models.DateTimeField(default=timezone.now, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "invoices_invoice"
        ordering = ["-delivered_at", "-id"]
        constraints = [
            models.CheckConstraint(condition=Q(total__gte=0), name="invoice_total_non_negative"),
            models.CheckConstraint(condition=Q(paid__gte=0), name="invoice_paid_non_negative"),
            models.CheckConstraint(condition=Q(returned__gte=0), name="invoice_returned_non_negative"),
            models.CheckConstraint(condition=Q(credited__gte=0), name="invoice_credited_non_negative"),
        ]

    @property
    def remaining(self) -> int:
        return self.total - self.paid - self.returned - self.credited

    @property
    def status_label(self) -> str:
        rem = self.remaining
        if rem <= 0:
            return "paid"
        if self.paid > 0 or self.returned > 0 or self.credited > 0:
            return "partial"
        return "open"

    @property
    def profit(self) -> int:
        return (self.total - self.returned - self.credited) - self.cost


class InvoiceLine(models.Model):
    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name="lines")
    warehouse = models.ForeignKey("warehouses.Warehouse", on_delete=models.PROTECT)
    item = models.ForeignKey("catalog.Item", on_delete=models.PROTECT)
    unit = models.CharField(max_length=16, choices=[("carton", "Carton"), ("piece", "Piece")])
    qty = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    pieces = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    returned_pieces = models.PositiveIntegerField(default=0)
    unit_price = models.BigIntegerField()
    offer_off = models.BigIntegerField(default=0)
    clearance = models.BooleanField(default=False)
    total = models.BigIntegerField()
    cost = models.BigIntegerField(default=0)

    class Meta:
        db_table = "invoices_line"
        ordering = ["id"]


class InvoiceBatchAllocation(models.Model):
    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name="batch_allocations")
    invoice_line = models.ForeignKey(InvoiceLine, on_delete=models.CASCADE, related_name="batch_allocations")
    batch = models.ForeignKey("inventory.Batch", on_delete=models.PROTECT, related_name="invoice_allocations")
    customer = models.ForeignKey("customers.Customer", on_delete=models.PROTECT, related_name="batch_allocations")
    qty_pieces = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    returned_pieces = models.PositiveIntegerField(default=0)
    purchase_price = models.BigIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "invoices_batch_allocation"
        ordering = ["id"]
        constraints = [
            models.CheckConstraint(
                condition=Q(returned_pieces__lte=models.F("qty_pieces")),
                name="alloc_returned_lte_qty",
            ),
        ]


class CreditNote(models.Model):
    number = models.CharField(max_length=40, unique=True, db_index=True)
    invoice = models.ForeignKey(Invoice, on_delete=models.PROTECT, related_name="credit_notes")
    customer = models.ForeignKey("customers.Customer", on_delete=models.PROTECT, related_name="credit_notes")
    amount = models.BigIntegerField(validators=[MinValueValidator(1)])
    reason = models.TextField()
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="credit_notes")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "invoices_credit_note"
        ordering = ["-id"]
        constraints = [
            models.CheckConstraint(condition=Q(amount__gt=0), name="credit_note_amount_gt_zero"),
        ]


class ReturnDoc(models.Model):
    number = models.CharField(max_length=40, unique=True, db_index=True)
    invoice = models.ForeignKey(Invoice, on_delete=models.PROTECT, related_name="returns")
    customer = models.ForeignKey("customers.Customer", on_delete=models.PROTECT, related_name="returns")
    rep = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="rep_returns"
    )
    warehouse = models.ForeignKey("warehouses.Warehouse", on_delete=models.PROTECT, related_name="returns")
    amount = models.BigIntegerField(default=0)
    cost_recovered = models.BigIntegerField(default=0)
    commission_base = models.BigIntegerField(default=0)
    note = models.TextField(blank=True, default="")
    client_ref = models.CharField(max_length=120, unique=True, null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="created_returns")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "invoices_return"
        ordering = ["-id"]
        constraints = [
            models.CheckConstraint(condition=Q(amount__gte=0), name="return_amount_non_negative"),
            models.CheckConstraint(condition=Q(commission_base__gte=0), name="return_commission_base_non_negative"),
        ]


class ReturnLine(models.Model):
    return_doc = models.ForeignKey(ReturnDoc, on_delete=models.CASCADE, related_name="lines")
    invoice_line = models.ForeignKey(InvoiceLine, on_delete=models.SET_NULL, null=True, blank=True)
    item = models.ForeignKey("catalog.Item", on_delete=models.PROTECT)
    batch = models.ForeignKey("inventory.Batch", on_delete=models.SET_NULL, null=True, blank=True)
    condition = models.CharField(max_length=16, choices=ReturnCondition.choices, default=ReturnCondition.GOOD)
    unit = models.CharField(max_length=16, choices=[("carton", "Carton"), ("piece", "Piece")], default="piece")
    qty = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    pieces = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    unit_price = models.BigIntegerField(default=0)
    amount = models.BigIntegerField(default=0)
    cost = models.BigIntegerField(default=0)

    class Meta:
        db_table = "invoices_return_line"
        ordering = ["id"]
