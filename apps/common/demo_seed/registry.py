"""Deterministic DemoSeedRecord registry, clock-shifting context, and safe topological cleanup."""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch

from django.db import connection, transaction
from django.db.models import Model, Q

from apps.common.models import DemoSeedRecord

SEED_VERSION = "2026-11-12-v1"


def _coerce_object_id(pk: Any) -> int:
    try:
        return int(pk)
    except (TypeError, ValueError):
        return 0


def register_seed(seed_key: str, instance: Model, extra: dict[str, Any] | None = None) -> DemoSeedRecord:
    """Record or update ownership metadata for a synthetically seeded ORM row."""
    del extra  # Metadata carried cleanly via deterministic seed_key & ORM record
    obj, _ = DemoSeedRecord.objects.update_or_create(
        seed_key=seed_key,
        defaults={
            "profile": "full",
            "app_label": instance._meta.app_label,
            "model_name": instance._meta.model_name,
            "object_id": _coerce_object_id(instance.pk),
        },
    )
    return obj


def register_raw_seed(
    seed_key: str,
    *,
    app_label: str,
    model_name: str,
    object_id: int | str,
    extra: dict[str, Any] | None = None,
) -> DemoSeedRecord:
    """Register ownership when an ORM instance comes back as a serialized dict from a service call."""
    del extra
    obj, _ = DemoSeedRecord.objects.update_or_create(
        seed_key=seed_key,
        defaults={
            "profile": "full",
            "app_label": app_label,
            "model_name": model_name,
            "object_id": _coerce_object_id(object_id),
        },
    )
    return obj


def get_seeded[TModel: Model](seed_key: str, model_cls: type[TModel]) -> TModel | None:
    """Fetch a previously seeded model row if it still exists in the database."""
    rec = DemoSeedRecord.objects.filter(
        seed_key=seed_key,
        app_label=model_cls._meta.app_label,
        model_name=model_cls._meta.model_name,
    ).first()
    if not rec:
        return None
    if rec.object_id > 0:
        return model_cls.objects.filter(pk=rec.object_id).first()
    return None


def seeded_int_ids(app_label: str, model_name: str) -> list[int]:
    """Return all integer PKs tracked in DemoSeedRecord for a model."""
    return [
        int(oid)
        for oid in DemoSeedRecord.objects.filter(app_label=app_label, model_name=model_name, object_id__gt=0)
        .values_list("object_id", flat=True)
    ]


def seeded_str_ids(app_label: str, model_name: str) -> list[str]:
    return [
        sk.split(":", 1)[1]
        for sk in DemoSeedRecord.objects.filter(app_label=app_label, model_name=model_name)
        .values_list("seed_key", flat=True)
        if ":" in sk
    ]


@contextmanager
def frozen_now(target_dt: datetime) -> Iterator[None]:
    """Patch `django.utils.timezone.now` so `auto_now_add` fields stamp realistic historical dates on INSERT."""
    dt = target_dt if target_dt.tzinfo else target_dt.replace(tzinfo=UTC)
    with patch("django.utils.timezone.now", return_value=dt):
        yield


@transaction.atomic
def clear_demo_data(*, delete_media: bool = True) -> dict[str, int]:
    """Delete ONLY records tracked in DemoSeedRecord in topological order, leaving manual records untouched."""
    from apps.accounts.models import (
        CustomFieldValue,
        LoginAttempt,
        PriceTier,
        RuleRun,
        SiteConfig,
        User,
        UserSession,
    )
    from apps.audit.models import AuditLog
    from apps.catalog.models import Category, Item, PriceChange
    from apps.chat.models import ChatMessage, ChatReadCursor
    from apps.commissions.models import ClosedCommissionMonth
    from apps.common.models import StorageObject
    from apps.customers.models import Customer
    from apps.deliveries.models import Delivery, RepLocation, Vehicle
    from apps.exchange_rates.models import UsdRate
    from apps.expenses.models import Expense
    from apps.inventory.models import Batch, DamagedInventory, InventoryMovement
    from apps.invoices.models import CreditNote, Invoice, ReturnDoc
    from apps.notifications.models import Notification, OutboxEvent
    from apps.orders.models import Order
    from apps.payments.models import OnlinePayment, Payment, PaymentAllocation, Refund
    from apps.receipts.models import Receipt
    from apps.stocktakes.models import Stocktake
    from apps.supply_orders.models import SupplyOrder
    from apps.transfers.models import Transfer
    from apps.warehouses.models import Warehouse
    from integrations.storage import get_storage

    if connection.vendor == "postgresql":
        # PostgreSQL append-only triggers exist by design; prevent silent bypasses.
        raise RuntimeError(
            "clear_demo_data cannot delete immutable ledger rows on PostgreSQL when immutability triggers are active; reset schema or seed onto a fresh staging DB."
        )

    counts: dict[str, int] = {}

    def _del(label: str, qs) -> None:
        deleted, _ = qs.delete()
        if deleted:
            counts[label] = counts.get(label, 0) + deleted

    seed_orders = seeded_int_ids("orders", "order")
    seed_invoices = seeded_int_ids("invoices", "invoice")
    seed_payments = seeded_int_ids("payments", "payment")
    seed_customers = seeded_int_ids("customers", "customer")
    # Include service-generated child rest_orders / split shipments belonging to seeded customers/orders
    if seed_orders or seed_customers:
        seed_orders = list(
            Order.objects.filter(
                Q(id__in=seed_orders)
                | Q(customer_id__in=seed_customers)
                | Q(shipment_of_id__in=seed_orders)
            )
            .values_list("id", flat=True)
        )
    if seed_invoices or seed_customers or seed_orders:
        seed_invoices = list(
            Invoice.objects.filter(
                Q(id__in=seed_invoices)
                | Q(customer_id__in=seed_customers)
                | Q(order_id__in=seed_orders)
            )
            .values_list("id", flat=True)
        )
    seed_items = seeded_int_ids("catalog", "item")
    seed_warehouses = seeded_int_ids("warehouses", "warehouse")
    seed_users = seeded_int_ids("accounts", "user")
    seed_receipts = seeded_int_ids("receipts", "receipt")
    seed_transfers = seeded_int_ids("transfers", "transfer")
    seed_stocktakes = seeded_int_ids("stocktakes", "stocktake")
    seed_supply = seeded_int_ids("supply_orders", "supplyorder")
    seed_expenses = seeded_int_ids("expenses", "expense")
    seed_vehicles = seeded_int_ids("deliveries", "vehicle")

    # Collect seeded storage keys BEFORE deleting entities so manual uploads are never touched
    seeded_pub_keys: list[str] = []
    seeded_priv_keys: list[str] = []
    if delete_media:
        if DemoSeedRecord.objects.filter(seed_key="media:company:logo").exists():
            cfg_logo = SiteConfig.objects.filter(pk=1).values_list("brand_logo_path", flat=True).first()
            if cfg_logo:
                seeded_pub_keys.append(cfg_logo)
        if seed_items:
            seeded_pub_keys.extend(Item.objects.filter(id__in=seed_items).exclude(image_path="").values_list("image_path", flat=True))
        if seed_users:
            seeded_pub_keys.extend(User.objects.filter(id__in=seed_users).exclude(photo_path="").values_list("photo_path", flat=True))
        if seed_orders:
            seeded_priv_keys.extend(Delivery.objects.filter(order_id__in=seed_orders).exclude(photo_path="").values_list("photo_path", flat=True))
        if seed_expenses:
            seeded_priv_keys.extend(Expense.objects.filter(id__in=seed_expenses).exclude(photo_path="").values_list("photo_path", flat=True))

    # 1. Financials (reversals -> allocations -> online payments -> payments -> refunds -> credit notes -> returns -> invoices)
    if seed_payments:
        _del("payment_allocations", PaymentAllocation.objects.filter(Q(payment_id__in=seed_payments) | Q(payment__reversal_of_id__in=seed_payments)))
        _del("payment_reversals", Payment.objects.filter(reversal_of_id__in=seed_payments))
    if seed_invoices:
        _del("invoice_allocations", PaymentAllocation.objects.filter(invoice_id__in=seed_invoices))
    _del("online_payments", OnlinePayment.objects.filter(customer_id__in=seed_customers))
    _del("payments", Payment.objects.filter(id__in=seed_payments))
    _del("refunds", Refund.objects.filter(id__in=seeded_int_ids("payments", "refund")))
    _del("credit_notes", CreditNote.objects.filter(Q(id__in=seeded_int_ids("invoices", "creditnote")) | Q(invoice_id__in=seed_invoices)))
    _del("returns", ReturnDoc.objects.filter(Q(id__in=seeded_int_ids("invoices", "returndoc")) | Q(invoice_id__in=seed_invoices)))
    _del("invoices", Invoice.objects.filter(id__in=seed_invoices))

    # 2. Fulfillment & orders
    if seed_orders:
        Order.objects.filter(id__in=seed_orders).update(rest_order=None, shipment_of=None)
        _del("deliveries", Delivery.objects.filter(order_id__in=seed_orders))
        _del("orders", Order.objects.filter(id__in=seed_orders))
    if seed_users:
        _del("rep_locations", RepLocation.objects.filter(rep_id__in=seed_users))

    # 3. Stocktakes / transfers / receipts / supply orders / inventory batches
    _del("stocktakes", Stocktake.objects.filter(id__in=seed_stocktakes))
    _del("transfers", Transfer.objects.filter(id__in=seed_transfers))
    # Collect batches owned or derived from seeded receipts/transfers before clearing movements & receipts
    batch_q = Batch.objects.filter(
        Q(id__in=seeded_int_ids("inventory", "batch"))
        | Q(receipt_id__in=seed_receipts)
        | Q(origin_batch__receipt_id__in=seed_receipts)
        | Q(warehouse_id__in=seed_warehouses, item_id__in=seed_items)
    ).distinct()
    seeded_batch_ids = list(batch_q.values_list("id", flat=True))
    _del(
        "inventory_movements",
        InventoryMovement.objects.filter(
            Q(batch_id__in=seeded_batch_ids)
            | Q(warehouse_id__in=seed_warehouses, item_id__in=seed_items)
            | Q(ref_type__in=("receipt", "transfer", "stocktake", "order", "invoice", "return"), ref_id__in=[str(x) for x in (seed_receipts + seed_transfers + seed_stocktakes + seed_orders + seed_invoices)])
        ),
    )
    _del("damaged_inventory", DamagedInventory.objects.filter(Q(warehouse_id__in=seed_warehouses, item_id__in=seed_items)))
    _del("receipts", Receipt.objects.filter(id__in=seed_receipts))
    if seeded_batch_ids:
        Batch.objects.filter(id__in=seeded_batch_ids).update(origin_batch=None)
    _del("batches", Batch.objects.filter(id__in=seeded_batch_ids))
    _del("supply_orders", SupplyOrder.objects.filter(id__in=seed_supply))

    # 4. Expenses, commissions, chat, notifications, rules, audit
    _del("expenses", Expense.objects.filter(id__in=seed_expenses))
    closed_comm_ids = seeded_int_ids("commissions", "closedcommissionmonth")
    if closed_comm_ids:
        _del("closed_commissions", ClosedCommissionMonth.objects.filter(id__in=closed_comm_ids))
    if seed_customers:
        _del("chat_cursors", ChatReadCursor.objects.filter(customer_id__in=seed_customers))
        _del("chat_messages", ChatMessage.objects.filter(customer_id__in=seed_customers))
    notif_ids = seeded_int_ids("notifications", "notification")
    if notif_ids:
        _del("notifications", Notification.objects.filter(id__in=notif_ids))
    if seed_users:
        _del("notifications_by_user", Notification.objects.filter(user_id__in=seed_users))
        _del("login_attempts", LoginAttempt.objects.filter(user_id__in=seed_users))
        _del("user_sessions", UserSession.objects.filter(user_id__in=seed_users))
        _del("audit_logs", AuditLog.objects.filter(actor_id__in=seed_users))
    _del("outbox_events", OutboxEvent.objects.filter(topic__startswith="demo."))
    _del("rule_runs", RuleRun.objects.filter(id__in=seeded_int_ids("accounts", "rulerun")))
    _del("vehicles", Vehicle.objects.filter(id__in=seed_vehicles))

    # 5. Master entities (custom fields -> customers -> items -> categories -> tiers -> users -> warehouses -> usd_rates)
    if seed_orders:
        _del("custom_order", CustomFieldValue.objects.filter(object_type="order", object_id__in=seed_orders))
    if seed_customers:
        _del("custom_customer", CustomFieldValue.objects.filter(object_type="customer", object_id__in=seed_customers))
        _del("customers", Customer.objects.filter(id__in=seed_customers))
    if seed_items:
        _del("custom_item", CustomFieldValue.objects.filter(object_type="item", object_id__in=seed_items))
        _del("price_changes", PriceChange.objects.filter(item_id__in=seed_items))
        _del("items", Item.objects.filter(id__in=seed_items))
    _del("categories", Category.objects.filter(id__in=seeded_int_ids("catalog", "category")))
    _del("price_tiers", PriceTier.objects.filter(id__in=seeded_int_ids("accounts", "pricetier")))
    usd_ids = seeded_int_ids("exchange_rates", "usdrate")
    if usd_ids:
        _del("usd_rates", UsdRate.objects.filter(id__in=usd_ids))
    if seed_users:
        _del("users", User.objects.filter(id__in=seed_users))
    _del("warehouses", Warehouse.objects.filter(id__in=seed_warehouses))

    # 6. Media cleanup strictly for seeded storage assets
    if delete_media:
        storage = get_storage()
        for key in seeded_pub_keys:
            storage.delete("product-images", key)
        for key in seeded_priv_keys:
            storage.delete("delivery-proofs", key)
        all_del_keys = seeded_pub_keys + seeded_priv_keys
        if all_del_keys:
            _del("storage_objects", StorageObject.objects.filter(object_path__in=all_del_keys))

    # Reset SiteConfig brand logo if it came from demo seed
    if DemoSeedRecord.objects.filter(seed_key="site:config").exists():
        cfg = SiteConfig.objects.filter(pk=1).first()
        if cfg:
            cfg.brand_logo_path = ""
            cfg.profiles = []
            cfg.apps = []
            cfg.fields = []
            cfg.rules = []
            cfg.save(update_fields=["brand_logo_path", "profiles", "apps", "fields", "rules"])

    _del("demo_seed_records", DemoSeedRecord.objects.all())
    return counts
