"""Customers models (Phase 2 core: Customer & ShopMembership; extended in Phase 5/6)."""
from __future__ import annotations

from django.conf import settings
from django.db import models
from django.db.models import Q


class CustomerStatus(models.TextChoices):
    ACTIVE = "active", "Active"
    PENDING = "pending", "Pending Approval"
    REJECTED = "rejected", "Rejected"


class Customer(models.Model):
    name = models.CharField(max_length=150)
    owner_name = models.CharField(max_length=150, blank=True, default="")
    phone = models.CharField(max_length=20, db_index=True)
    city = models.CharField(max_length=100)
    area = models.CharField(max_length=120)
    street = models.CharField(max_length=160)
    landmark = models.CharField(max_length=200)
    address_text = models.CharField(max_length=255, blank=True, default="")
    address_info = models.JSONField(null=True, blank=True)
    lat = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    lng = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    tier = models.ForeignKey(
        "accounts.PriceTier",
        on_delete=models.PROTECT,
        related_name="customers",
    )
    rep = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assigned_customers",
    )
    debt_limit = models.BigIntegerField(default=0)
    active = models.BooleanField(default=True, db_index=True)
    walk_in = models.BooleanField(default=False, db_index=True)
    status = models.CharField(
        max_length=20,
        choices=CustomerStatus.choices,
        default=CustomerStatus.ACTIVE,
        db_index=True,
    )
    reject_reason = models.TextField(blank=True, default="")
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="approved_customers",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "customers_customer"
        ordering = ["id"]
        constraints = [
            models.CheckConstraint(condition=Q(debt_limit__gte=0), name="customer_debt_limit_non_negative"),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.phone})"


class ShopMembership(models.Model):
    """Links a shop user account to one or more Customer shops and tracks active shop."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="shop_memberships",
    )
    customer = models.ForeignKey(
        Customer,
        on_delete=models.CASCADE,
        related_name="memberships",
    )
    is_primary = models.BooleanField(default=False)
    is_active_shop = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "customers_shop_membership"
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(fields=["user", "customer"], name="uniq_shop_membership"),
            models.UniqueConstraint(
                fields=["user"],
                condition=Q(is_active_shop=True),
                name="uniq_user_active_shop",
            ),
        ]


class SpecialPrice(models.Model):
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, related_name="special_prices")
    item = models.ForeignKey("catalog.Item", on_delete=models.CASCADE, related_name="special_prices")
    carton_price = models.BigIntegerField()
    piece_price = models.BigIntegerField()
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "customers_special_price"
        constraints = [
            models.UniqueConstraint(fields=["customer", "item"], name="uniq_customer_item_special_price"),
            models.CheckConstraint(condition=Q(carton_price__gte=0), name="special_price_carton_non_negative"),
            models.CheckConstraint(condition=Q(piece_price__gte=0), name="special_price_piece_non_negative"),
        ]


class ShopList(models.Model):
    """Saved customer shop item lists (max 30 lists per shop, up to 80 lines each)."""

    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, related_name="shop_lists")
    name = models.CharField(max_length=40)
    lines = models.JSONField(default=list)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "customers_shop_list"
        ordering = ["-updated_at", "-id"]

