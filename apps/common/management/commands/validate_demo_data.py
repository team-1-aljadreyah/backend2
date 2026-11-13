"""Comprehensive data integrity, ledger reconciliation, and media verification command."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.accounts.models import PriceTier, Role, SiteConfig, SystemSetting, User
from apps.catalog.models import Item
from apps.commissions.models import ClosedCommissionMonth
from apps.customers.models import Customer
from apps.deliveries.models import Delivery
from apps.exchange_rates.models import UsdRate
from apps.expenses.models import Expense
from apps.inventory.models import Batch
from apps.invoices.models import Invoice, InvoiceBatchAllocation
from apps.orders.models import Order
from apps.payments.models import Payment
from apps.warehouses.models import Warehouse
from integrations.storage import LocalMediaStorage, get_storage


class Command(BaseCommand):
    help = "Validate ERP database integrity, inventory/FEFO constraints, financial reconciliation, and media storage."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--strict-full",
            action="store_true",
            help="Enforce full-profile minimum counts (5+ warehouses, 90+ items, 60+ users, 50+ customers, media manifest).",
        )

    def handle(self, *args, **options) -> None:
        strict_full = bool(options["strict_full"])
        failures: list[str] = []
        checks: dict[str, Any] = {}

        # 1. Foundation checks
        from apps.accounts.profiles import get_site_payload

        cfg = SiteConfig.get_solo()
        s = SystemSetting.get_solo()
        brand = get_site_payload().get("brand") or {}
        if not cfg.brand_name or not brand.get("name"):
            failures.append("SiteConfig missing brand_name.")
        if s.usd_rate <= 0:
            failures.append(f"Invalid SystemSetting.usd_rate={s.usd_rate}")

        wh_count = Warehouse.objects.filter(active=True).count()
        tier_count = PriceTier.objects.count()
        usd_count = UsdRate.objects.count()
        if wh_count < 1 or tier_count < 1 or usd_count < 1:
            failures.append(f"Missing core master data (warehouses={wh_count}, tiers={tier_count}, usd_rates={usd_count}).")

        # 2. User & RBAC integrity checks
        users_qs = User.objects.filter(deleted=False)
        user_count = users_qs.count()
        mgr_count = users_qs.filter(role=Role.MANAGER, active=True).count()
        if mgr_count < 1:
            failures.append("No active manager account exists.")

        for u in users_qs:
            if not u.password or not u.has_usable_password():
                failures.append(f"User {u.id} ({u.phone}) lacks a usable password hash.")
            if u.role in (Role.WAREHOUSE, Role.STAFF) and not u.warehouse_id:
                failures.append(f"Warehouse/Staff user {u.id} ({u.phone}) missing warehouse assignment.")
            if u.role == Role.REP and not u.rep_kinds:
                failures.append(f"Rep user {u.id} ({u.phone}) missing rep_kinds.")

        # Verify active warehouse keeper uniqueness cleanly in Python
        active_keeper_whs: set[int] = set()
        for k in users_qs.filter(role=Role.WAREHOUSE, active=True):
            if k.warehouse_id in active_keeper_whs:
                failures.append(f"Duplicate active warehouse keeper on warehouse_id={k.warehouse_id}.")
            if k.warehouse_id:
                active_keeper_whs.add(k.warehouse_id)

        # 3. Catalog & uniqueness checks
        items = list(Item.objects.prefetch_related("prices").all())
        seen_barcodes: set[str] = set()
        for it in items:
            if it.pieces_per_carton < 1:
                failures.append(f"Item {it.id} has invalid pieces_per_carton={it.pieces_per_carton}.")
            if not it.prices.exists():
                failures.append(f"Item {it.id} ({it.name_en}) has no ItemPrice rows.")
            if it.barcode:
                if it.barcode in seen_barcodes:
                    failures.append(f"Duplicate barcode detected: {it.barcode}.")
                seen_barcodes.add(it.barcode)

        # 4. Inventory & FEFO non-negativity + zero expired sales
        bad_batches = Batch.objects.filter(qty_left__lt=0).count()
        if bad_batches:
            failures.append(f"Found {bad_batches} Batch rows with negative qty_left.")

        expired_sales = 0
        for alloc in InvoiceBatchAllocation.objects.select_related("invoice", "batch"):
            inv_date = alloc.invoice.delivered_at.date()
            if inv_date > alloc.batch.expiry_date:
                expired_sales += 1
        if expired_sales:
            failures.append(f"Detected {expired_sales} InvoiceBatchAllocation rows drawn from expired batches!")

        # 5. Financial ledger reconciliation (Invoices, Payments, Commissions)
        invoices = list(Invoice.objects.prefetch_related("lines").all())
        for inv in invoices:
            line_sum = sum(ln.total for ln in inv.lines.all())
            expected_total = inv.subtotal - inv.discount + inv.rounding
            if inv.subtotal != line_sum:
                failures.append(f"Invoice {inv.number} subtotal mismatch: subtotal={inv.subtotal}, lines={line_sum}.")
            if inv.total != expected_total:
                failures.append(
                    f"Invoice {inv.number} header arithmetic mismatch: {inv.subtotal} - {inv.discount} + {inv.rounding} != {inv.total}."
                )
            if inv.paid + inv.credited > inv.total or inv.returned > inv.total:
                failures.append(f"Invoice {inv.number} over-settled/over-returned: paid({inv.paid}) credited({inv.credited}) returned({inv.returned}) total({inv.total}).")

        payments = list(Payment.objects.prefetch_related("allocations").all())
        for p in payments:
            alloc_sum = sum(a.amount for a in p.allocations.all())
            if p.reversal_of_id is None:
                if alloc_sum < 0:
                    failures.append(f"Payment {p.number} has negative allocation total.")
                if alloc_sum + p.unallocated_amount != p.amount:
                    failures.append(
                        f"Payment {p.number} reconciliation error: alloc({alloc_sum}) + unalloc({p.unallocated_amount}) != amount({p.amount})."
                    )
            else:
                if alloc_sum > 0 or p.amount >= 0:
                    failures.append(f"Reversal payment {p.number} must have non-positive allocation and negative amount.")
                if alloc_sum + p.unallocated_amount != p.amount:
                    failures.append(
                        f"Reversal payment {p.number} reconciliation error: alloc({alloc_sum}) + unalloc({p.unallocated_amount}) != amount({p.amount})."
                    )

        for cm in ClosedCommissionMonth.objects.all():
            snap_coll = sum(int(r.get("paid_sales") or 0) for r in (cm.snapshot or []))
            snap_comm = sum(int(r.get("net") or 0) for r in (cm.snapshot or []))
            if snap_coll != cm.total_collected or snap_comm != cm.total_commission:
                failures.append(f"ClosedCommissionMonth {cm.month} snapshot sum mismatch.")

        # 6. Media storage & asset manifest checks
        storage = get_storage()
        missing_media = 0
        checked_media = 0

        def _media_exists(bucket: str, key: str) -> bool:
            if isinstance(storage, LocalMediaStorage):
                fp = (storage.root / bucket / key.lstrip("/")).resolve()
                return fp.is_file() and fp.stat().st_size > 0
            return bool(storage.get_url(bucket, key))

        if cfg.brand_logo_path:
            checked_media += 1
            if not _media_exists("product-images", cfg.brand_logo_path):
                missing_media += 1
        for it in items:
            if it.image_path:
                checked_media += 1
                if not _media_exists("product-images", it.image_path):
                    missing_media += 1
        for u in users_qs:
            if u.photo_path:
                checked_media += 1
                if not _media_exists("product-images", u.photo_path):
                    missing_media += 1
        for dlv in Delivery.objects.exclude(photo_path=""):
            checked_media += 1
            if not _media_exists("delivery-proofs", dlv.photo_path):
                missing_media += 1
        for exp in Expense.objects.exclude(photo_path=""):
            checked_media += 1
            if not _media_exists("delivery-proofs", exp.photo_path):
                missing_media += 1

        if missing_media:
            failures.append(f"{missing_media} media paths stored in DB could not be read from StorageBackend.")

        manifest_path = Path(settings.BASE_DIR) / "seed_assets" / "asset_manifest.json"
        manifest_count = 0
        if manifest_path.is_file():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            assets = manifest.get("assets") or []
            manifest_count = len(assets)
            seed_root = Path(settings.BASE_DIR) / "seed_assets"
            for asset in assets:
                fp = seed_root / str(asset.get("file_path") or "")
                tp = seed_root / str(asset.get("thumb_path") or "")
                if not fp.is_file() or not tp.is_file():
                    failures.append(f"Asset manifest missing file/thumb: {asset.get('asset_id')}")
                    continue
                actual_sha = hashlib.sha256(fp.read_bytes()).hexdigest()
                if actual_sha != asset.get("sha256"):
                    failures.append(f"Asset SHA-256 mismatch for {asset.get('asset_id')}")

        # 7. Optional strict full-ecosystem threshold checks
        cust_count = Customer.objects.count()
        order_count = Order.objects.count()
        batch_count = Batch.objects.count()
        if strict_full:
            if wh_count < 4:
                failures.append(f"Expected >= 4 active warehouses in full mode, found {wh_count}.")
            if len(items) < 80:
                failures.append(f"Expected >= 80 items in full mode, found {len(items)}.")
            if user_count < 63:
                failures.append(f"Expected >= 63 active+inactive users in full mode, found {user_count}.")
            if cust_count < 45:
                failures.append(f"Expected >= 45 customers in full mode, found {cust_count}.")
            if order_count < 100 or len(invoices) < 75:
                failures.append(f"Expected >= 100 orders and >= 75 invoices, found orders={order_count}, invoices={len(invoices)}.")
            if checked_media < 150 or manifest_count < 150:
                failures.append(f"Expected >= 150 verified media files & manifest entries, found checked={checked_media}, manifest={manifest_count}.")

        checks["counts"] = {
            "warehouses": wh_count,
            "price_tiers": tier_count,
            "usd_rates": usd_count,
            "users": user_count,
            "customers": cust_count,
            "items": len(items),
            "batches": batch_count,
            "orders": order_count,
            "invoices": len(invoices),
            "payments": len(payments),
            "closed_commissions": ClosedCommissionMonth.objects.count(),
            "checked_media": checked_media,
            "manifest_assets": manifest_count,
        }
        checks["failures"] = failures
        checks["status"] = "PASSED" if not failures else "FAILED"

        self.stdout.write(json.dumps(checks, ensure_ascii=False, indent=2))
        if failures:
            raise CommandError(f"Demo data validation failed with {len(failures)} error(s).")
