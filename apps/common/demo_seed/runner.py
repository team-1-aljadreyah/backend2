"""Top-level orchestrator for full synthetic ERP demo ecosystem generation & reporting."""
from __future__ import annotations

from typing import Any

from apps.accounts.models import Role, User
from apps.common.demo_seed.catalog_data import ITEM_CATALOG, WAREHOUSES, seed_catalog_foundation
from apps.common.demo_seed.media_bind import bind_demo_media
from apps.common.demo_seed.operations_data import seed_operations
from apps.common.demo_seed.people_data import (
    ensure_primary_manager,
    seed_people_and_network,
)


def dry_run_summary(*, with_media: bool = True) -> dict[str, Any]:
    """Return exact planned counts without touching the database."""
    return {
        "profile": "full",
        "dry_run": True,
        "planned_entities": {
            "warehouses": len(WAREHOUSES),
            "price_tiers": 4,
            "categories": 10,
            "items": len(ITEM_CATALOG),
            "usd_rates": 40,
            "users": 68,
            "customers": 52,
            "vehicles": 5,
            "supply_orders": 10,
            "receipts": 24,
            "transfers": 14,
            "stocktakes": 5,
            "orders": "126+",
            "invoices": "90+",
            "payments": "74+",
            "returns": 12,
            "credit_notes": 8,
            "refunds": 6,
            "expenses": 28,
            "closed_commissions": 2,
            "chat_threads": 18,
            "media_assets": (1 + len(ITEM_CATALOG) + 68 + 28 + 38) if with_media else 0,
        },
    }


def run_full_seed(*, with_media: bool = True, dry_run: bool = False) -> dict[str, Any]:
    """Populate the complete realistic Al-Nahrain Wholesale ERP demo environment idempotently."""
    if dry_run:
        return dry_run_summary(with_media=with_media)

    manager = ensure_primary_manager()
    cat_ctx = seed_catalog_foundation(manager, with_media=with_media)
    ppl_ctx = seed_people_and_network(
        manager,
        warehouses=cat_ctx["warehouse_list"],
        tiers=cat_ctx["tiers"],
        items=cat_ctx["items"],
    )
    ops_ctx = seed_operations(
        manager=manager,
        accountants=ppl_ctx["accountants"],
        keepers=ppl_ctx["keepers"],
        staff_users=ppl_ctx["staff_users"],
        sales_reps=ppl_ctx["sales_reps"],
        delivery_reps=ppl_ctx["delivery_reps"],
        warehouses=cat_ctx["warehouse_list"],
        items=cat_ctx["items"],
        item_costs=cat_ctx["item_costs"],
        active_customers=ppl_ctx["active_customers"],
        vehicles=ppl_ctx["vehicles"],
        with_media=with_media,
    )

    media_ctx: dict[str, Any] = {"skipped": not with_media}
    if with_media and not ops_ctx.get("already_seeded"):
        media_ctx = bind_demo_media(
            items=cat_ctx["items"],
            users=ppl_ctx["all_users"],
            logo_meta=cat_ctx.get("logo_meta"),
            proof_assets=ops_ctx.get("proof_assets"),
            expense_assets=ops_ctx.get("expense_assets"),
        )
    elif with_media and ops_ctx.get("already_seeded"):
        # Ensure manifest + bindings exist even on idempotent rerun
        all_users = list(User.objects.filter(deleted=False).order_by("id"))
        media_ctx = bind_demo_media(
            items=cat_ctx["items"],
            users=all_users,
            logo_meta=cat_ctx.get("logo_meta"),
        )

    from apps.customers.models import Customer
    from apps.expenses.models import Expense
    from apps.inventory.models import Batch
    from apps.invoices.models import CreditNote, Invoice, ReturnDoc
    from apps.orders.models import Order
    from apps.payments.models import Payment, Refund
    from apps.stocktakes.models import Stocktake
    from apps.supply_orders.models import SupplyOrder
    from apps.transfers.models import Transfer

    return {
        "profile": "full",
        "already_seeded": bool(ops_ctx.get("already_seeded")),
        "counts": {
            "users_total": User.objects.filter(deleted=False).count(),
            "managers": User.objects.filter(deleted=False, role=Role.MANAGER).count(),
            "warehouses": len(cat_ctx["warehouse_list"]),
            "items": len(cat_ctx["items"]),
            "batches": Batch.objects.count(),
            "customers": Customer.objects.count(),
            "supply_orders": SupplyOrder.objects.count(),
            "transfers": Transfer.objects.count(),
            "stocktakes": Stocktake.objects.count(),
            "orders": Order.objects.count(),
            "invoices": Invoice.objects.count(),
            "payments": Payment.objects.count(),
            "returns": ReturnDoc.objects.count(),
            "credit_notes": CreditNote.objects.count(),
            "refunds": Refund.objects.count(),
            "expenses": Expense.objects.count(),
        },
        "media": media_ctx,
    }
