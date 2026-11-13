"""Accounts domain models: User, PriceTier, SystemSetting, RevokedToken."""
from __future__ import annotations

from decimal import Decimal

from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q


class Role(models.TextChoices):
    MANAGER = "manager", "Manager"
    ACCOUNTANT = "accountant", "Accountant"
    WAREHOUSE = "warehouse", "Warehouse Keeper"
    STAFF = "staff", "Warehouse Staff"
    REP = "rep", "Representative"
    SHOP = "shop", "Shop Owner"


class RepKind:
    SALES = "sales"
    DELIVERY = "delivery"
    ALL = (SALES, DELIVERY)


COST_ROLES = frozenset({Role.MANAGER, Role.ACCOUNTANT, Role.WAREHOUSE})
PROFIT_ROLES = frozenset({Role.MANAGER, Role.ACCOUNTANT})
DEBT_ROLES = frozenset({Role.MANAGER, Role.ACCOUNTANT, Role.REP, Role.SHOP})


class UserManager(BaseUserManager["User"]):
    use_in_migrations = True

    def create_user(self, phone: str, password: str | None = None, **extra_fields) -> User:
        if not phone:
            raise ValueError("Phone number is required.")
        user = self.model(phone=phone, **extra_fields)
        if password:
            user.set_password(password)
        else:
            user.set_unusable_password()
        user.save(using=self._db)
        return user

    def create_superuser(self, phone: str, password: str, **extra_fields) -> User:
        extra_fields.setdefault("role", Role.MANAGER)
        extra_fields.setdefault("active", True)
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        return self.create_user(phone=phone, password=password, **extra_fields)


class User(AbstractBaseUser, PermissionsMixin):
    name = models.CharField(max_length=150)
    phone = models.CharField(max_length=20, unique=True, db_index=True)
    role = models.CharField(max_length=20, choices=Role.choices, db_index=True)
    active = models.BooleanField(default=True, db_index=True)
    deleted = models.BooleanField(default=False, db_index=True)
    warehouse = models.ForeignKey(
        "warehouses.Warehouse",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assigned_users",
    )
    rep_kinds = models.JSONField(default=list, blank=True)
    rep_categories = models.JSONField(default=list, blank=True)
    photo_path = models.CharField(max_length=255, blank=True, default="")
    profile_id = models.CharField(max_length=64, null=True, blank=True)
    sales_target = models.BigIntegerField(null=True, blank=True)
    weak_password = models.BooleanField(default=False)
    token_version = models.PositiveIntegerField(default=1)
    is_staff = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = UserManager()

    USERNAME_FIELD = "phone"
    REQUIRED_FIELDS = ["name", "role"]

    class Meta:
        db_table = "accounts_user"
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(
                fields=["warehouse"],
                condition=Q(role=Role.WAREHOUSE, active=True, deleted=False),
                name="uniq_active_warehouse_keeper_per_warehouse",
            ),
        ]

    @property
    def is_active(self) -> bool:
        return bool(self.active and not self.deleted)

    def has_rep_kind(self, kind: str) -> bool:
        if self.role != Role.REP:
            return False
        kinds = self.rep_kinds or []
        # Legacy empty rep_kinds implies both sales and delivery capability (matching rules.ts)
        return len(kinds) == 0 or kind in kinds

    def __str__(self) -> str:
        return f"{self.name} ({self.role} · {self.phone})"


class PriceTier(models.Model):
    name = models.CharField(max_length=100)
    name_en = models.CharField(max_length=100)
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = "accounts_price_tier"
        ordering = ["sort_order", "id"]

    def __str__(self) -> str:
        return self.name_en


class SystemSetting(models.Model):
    """Singleton operational settings table (id=1)."""

    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    reservation_hours = models.PositiveIntegerField(default=24, validators=[MinValueValidator(1)])
    near_expiry_days = models.PositiveIntegerField(default=30, validators=[MinValueValidator(1)])
    slow_moving_days = models.PositiveIntegerField(default=30, validators=[MinValueValidator(1)])
    auto_confirm_rep_orders = models.BooleanField(default=False)
    commission_pct = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("2.00"))
    usd_rate = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("1500.00"))
    phone = models.CharField(max_length=40, default="07700000000")
    address = models.CharField(max_length=255, default="بغداد - جميلة التجارية")
    expense_approval_above = models.BigIntegerField(null=True, blank=True)
    summary_hour = models.PositiveSmallIntegerField(default=21)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "accounts_system_setting"

    @classmethod
    def get_solo(cls) -> SystemSetting:
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class UserSession(models.Model):
    """Per-login JWT session store isolating active shop context per device/login."""

    session_id = models.CharField(max_length=64, unique=True, db_index=True)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="auth_sessions")
    active_customer = models.ForeignKey(
        "customers.Customer",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="active_sessions",
    )
    refresh_jti = models.CharField(max_length=128, blank=True, default="")
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=255, blank=True, default="")
    revoked = models.BooleanField(default=False, db_index=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "accounts_user_session"
        indexes = [
            models.Index(fields=["user", "revoked"], name="accounts_us_user_revoked_idx"),
        ]


class RevokedAccessToken(models.Model):
    """Stores revoked JWT access token JTIs on logout until their natural expiration."""

    jti = models.CharField(max_length=64, unique=True, db_index=True)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="revoked_tokens")
    expires_at = models.DateTimeField(db_index=True)
    revoked_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "accounts_revoked_token"


class SiteConfig(models.Model):
    """Singleton tenant/white-label configuration, permission profiles, custom fields, and automation rules."""

    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    brand_name = models.CharField(max_length=150, blank=True, default="")
    brand_name_en = models.CharField(max_length=150, blank=True, default="")
    brand_tagline = models.CharField(max_length=255, blank=True, default="")
    brand_tagline_en = models.CharField(max_length=255, blank=True, default="")
    brand_logo_path = models.CharField(max_length=255, blank=True, default="")
    brand_color = models.CharField(max_length=32, blank=True, default="")
    brand_extra = models.JSONField(default=dict, blank=True)
    nav = models.JSONField(default=dict, blank=True)
    dashboard = models.JSONField(default=dict, blank=True)
    categories = models.JSONField(default=list, blank=True)
    texts = models.JSONField(default=dict, blank=True)
    layouts = models.JSONField(default=dict, blank=True)
    look = models.JSONField(default=dict, blank=True)
    profiles = models.JSONField(default=list, blank=True)
    apps = models.JSONField(default=list, blank=True)
    fields = models.JSONField(default=list, blank=True)
    rules = models.JSONField(default=list, blank=True)
    banners = models.JSONField(default=list, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "accounts_site_config"

    @classmethod
    def get_solo(cls) -> SiteConfig:
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class LoginAttempt(models.Model):
    """Authentication telemetry powering 24h failed-attempt health checks and lock monitoring."""

    phone = models.CharField(max_length=40, db_index=True)
    user = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name="login_attempts"
    )
    ok = models.BooleanField(default=False, db_index=True)
    reason = models.CharField(max_length=40, blank=True, default="")
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "accounts_login_attempt"
        ordering = ["-id"]
        indexes = [
            models.Index(fields=["created_at", "ok"], name="acc_login_att_created_ok_idx"),
        ]


class CustomFieldValue(models.Model):
    """Stores custom metadata values per business object (`customer`, `item`, `order`)."""

    object_type = models.CharField(
        max_length=24,
        choices=[("customer", "Customer"), ("item", "Item"), ("order", "Order")],
    )
    object_id = models.PositiveBigIntegerField()
    values = models.JSONField(default=dict, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "accounts_custom_field_value"
        constraints = [
            models.UniqueConstraint(
                fields=["object_type", "object_id"],
                name="uniq_custom_field_object",
            ),
        ]


class RuleRun(models.Model):
    """Execution history log for automated business rules."""

    rule_id = models.CharField(max_length=64, db_index=True)
    rule_name = models.CharField(max_length=160)
    trigger = models.CharField(max_length=40)
    record = models.CharField(max_length=120)
    link = models.CharField(max_length=255, blank=True, default="")
    did = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "accounts_rule_run"
        ordering = ["-id"]
