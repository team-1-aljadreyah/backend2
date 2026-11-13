"""Inventory, FEFO stock reservation/deduction, batch hold/release, and tracing services."""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from django.apps import apps
from django.db import transaction
from django.db.models import Min, Q, Sum
from django.db.models.functions import Coalesce
from rest_framework import status as http_status

from apps.accounts.models import Role, SystemSetting, User
from apps.accounts.permissions import own_warehouse_id
from apps.audit.services import record_audit
from apps.common.exceptions import ApiException, ErrorCode
from apps.common.utils import baghdad_today
from apps.inventory.models import Batch, DamagedInventory, InventoryMovement, MovementKind
from apps.inventory.serializers import serialize_batch, serialize_damaged
from apps.notifications.services import notify_role
from apps.warehouses.models import Warehouse


def is_count_locked(warehouse_id: int, category: str | None = None) -> bool:
    """Return True if an open stocktake locks this warehouse (full or category)."""
    if not apps.is_installed("apps.stocktakes"):
        return False
    from apps.stocktakes.models import Stocktake, StocktakeStatus

    qs = Stocktake.objects.filter(warehouse_id=warehouse_id, status=StocktakeStatus.OPEN)
    if not category:
        return qs.exists()
    return qs.filter(Q(category="") | Q(category__isnull=True) | Q(category=category)).exists()


def reserved_qty_map(
    *,
    warehouse_id: int | None = None,
    item_id: int | None = None,
    exclude_order_id: int | None = None,
) -> dict[tuple[int, int], int]:
    """Compute reserved pieces grouped by (warehouse_id, item_id) across active un-delivered orders."""
    if not apps.is_installed("apps.orders"):
        return {}
    from apps.orders.models import OrderLine, OrderStatus

    now = datetime.now(tz=UTC)
    held_q = Q(order__status=OrderStatus.APPROVED) | Q(
        order__status__in=[OrderStatus.PENDING, OrderStatus.NEEDS_APPROVAL],
        order__reserved_until__gt=now,
    )
    # Also note: if delivery exists in status ready/out/problem (not yet delivered/returned), order.status is still APPROVED!
    qs = OrderLine.objects.filter(held_q)
    if exclude_order_id is not None:
        qs = qs.exclude(order_id=exclude_order_id)
    if item_id is not None:
        qs = qs.filter(item_id=item_id)

    qs = qs.annotate(eff_wh=Coalesce("warehouse_id", "order__warehouse_id"))
    if warehouse_id is not None:
        qs = qs.filter(eff_wh=warehouse_id)

    rows = qs.values("eff_wh", "item_id").annotate(qty=Sum("pieces"))
    return {(r["eff_wh"], r["item_id"]): int(r["qty"] or 0) for r in rows}


def sellable_physical_map(
    *,
    warehouse_id: int | None = None,
    item_id: int | None = None,
) -> dict[tuple[int, int], int]:
    """Return unheld, non-expired physical stock per (warehouse_id, item_id)."""
    today = baghdad_today()
    qs = Batch.objects.filter(qty_left__gt=0, expiry_date__gte=today, held_at__isnull=True)
    if warehouse_id is not None:
        qs = qs.filter(warehouse_id=warehouse_id)
    if item_id is not None:
        qs = qs.filter(item_id=item_id)
    rows = qs.values("warehouse_id", "item_id").annotate(qty=Sum("qty_left"))
    return {(r["warehouse_id"], r["item_id"]): int(r["qty"] or 0) for r in rows}


def shelf_available(warehouse_id: int, item_id: int, *, exclude_order_id: int | None = None) -> int:
    phys = sellable_physical_map(warehouse_id=warehouse_id, item_id=item_id).get((warehouse_id, item_id), 0)
    res = reserved_qty_map(warehouse_id=warehouse_id, item_id=item_id, exclude_order_id=exclude_order_id).get(
        (warehouse_id, item_id), 0
    )
    return max(0, phys - res)


def build_item_stock_and_expiry_maps(*, exclude_order_id: int | None = None):
    """Return (stock_by_item, nearest_expiry_by_item, active_warehouses)."""
    today = baghdad_today()
    warehouses = list(Warehouse.objects.filter(active=True).order_by("id"))
    phys = sellable_physical_map()
    res = reserved_qty_map(exclude_order_id=exclude_order_id)

    expiry_rows = (
        Batch.objects.filter(qty_left__gt=0, expiry_date__gte=today, held_at__isnull=True)
        .values("item_id")
        .annotate(nearest=Min("expiry_date"))
    )
    nearest_map = {r["item_id"]: r["nearest"].isoformat() for r in expiry_rows if r["nearest"]}
    return phys, res, nearest_map, warehouses


def item_stock_dict(
    item_id: int,
    *,
    phys: dict[tuple[int, int], int],
    res: dict[tuple[int, int], int],
    warehouses: list[Warehouse],
) -> dict[str, Any]:
    by_wh = []
    total = 0
    for w in warehouses:
        avail = max(0, phys.get((w.id, item_id), 0) - res.get((w.id, item_id), 0))
        by_wh.append({"warehouse_id": w.id, "qty": avail})
        total += avail
    return {"total": total, "by_warehouse": by_wh}


def build_shelf_rows(*, actor: User, warehouse_id: int | None = None) -> list[dict[str, Any]]:
    from apps.catalog.models import Item

    own_wh = own_warehouse_id(actor)
    today = baghdad_today()
    warehouses = list(Warehouse.objects.filter(active=True).order_by("id"))
    if warehouse_id is not None:
        warehouses = [w for w in warehouses if w.id == warehouse_id]

    non_exp_unheld = sellable_physical_map(warehouse_id=warehouse_id)
    expired_rows = Batch.objects.filter(qty_left__gt=0, expiry_date__lt=today)
    if warehouse_id is not None:
        expired_rows = expired_rows.filter(warehouse_id=warehouse_id)
    expired_map = {
        (r["warehouse_id"], r["item_id"]): int(r["qty"] or 0)
        for r in expired_rows.values("warehouse_id", "item_id").annotate(qty=Sum("qty_left"))
    }
    res_map = reserved_qty_map(warehouse_id=warehouse_id)
    full_locked_whs = {w.id for w in warehouses if is_count_locked(w.id)}

    items = list(Item.objects.order_by("id").values_list("id", "category"))
    out = []
    for iid, cat in items:
        wh_rows = []
        for w in warehouses:
            live = non_exp_unheld.get((w.id, iid), 0)
            reserved = res_map.get((w.id, iid), 0)
            expired = expired_map.get((w.id, iid), 0)
            count_locked = w.id in full_locked_whs or is_count_locked(w.id, cat)
            sellable = 0 if count_locked else max(0, live - reserved)
            if own_wh is not None and w.id != own_wh:
                wh_rows.append(
                    {
                        "warehouse_id": w.id,
                        "on_shelf": sellable,
                        "sellable": sellable,
                        "reserved": 0,
                        "expired": 0,
                    }
                )
            else:
                wh_rows.append(
                    {
                        "warehouse_id": w.id,
                        "on_shelf": live + expired,
                        "sellable": sellable,
                        "reserved": max(0, live - sellable),
                        "expired": expired,
                    }
                )
        out.append({"item_id": iid, "by_warehouse": wh_rows})
    return out


def list_batches_for_actor(*, actor: User, item_id: int | None = None, warehouse_id: int | None = None) -> list[dict[str, Any]]:
    own_wh = own_warehouse_id(actor)
    if own_wh is not None:
        warehouse_id = own_wh

    qs = Batch.objects.filter(qty_left__gt=0).select_related("held_by").order_by("expiry_date", "id")
    if item_id is not None:
        qs = qs.filter(item_id=item_id)
    if warehouse_id is not None:
        qs = qs.filter(warehouse_id=warehouse_id)

    today = baghdad_today()
    near_days = SystemSetting.get_solo().near_expiry_days
    return [serialize_batch(b, actor=actor, today=today, near_days=near_days) for b in qs]


def list_damaged_for_actor(*, actor: User, warehouse_id: int | None = None) -> list[dict[str, Any]]:
    own_wh = own_warehouse_id(actor)
    if own_wh is not None:
        warehouse_id = own_wh
    qs = DamagedInventory.objects.order_by("-id")
    if warehouse_id is not None:
        qs = qs.filter(warehouse_id=warehouse_id)
    return [serialize_damaged(d) for d in qs]


def lock_and_deduct_fefo(
    *,
    warehouse_id: int,
    item_id: int,
    qty_pieces: int,
    actor: User | None = None,
    kind: str = MovementKind.SALE,
    ref_type: str = "",
    ref_id: int | str = "",
) -> list[tuple[Batch, int]]:
    """Atomically consume sellable stock FEFO (expiry ASC, id ASC) inside a transaction."""
    if qty_pieces <= 0:
        return []
    today = baghdad_today()
    batches = list(
        Batch.objects.select_for_update()
        .filter(
            warehouse_id=warehouse_id,
            item_id=item_id,
            qty_left__gt=0,
            expiry_date__gte=today,
            held_at__isnull=True,
        )
        .order_by("expiry_date", "id")
    )
    available = sum(b.qty_left for b in batches)
    if available < qty_pieces:
        raise ApiException(
            code=ErrorCode.OUT_OF_STOCK,
            message=("الكمية المتوفرة في الدفعات غير كافية", "Insufficient sellable batch stock"),
            status_code=http_status.HTTP_409_CONFLICT,
        )

    remaining = qty_pieces
    allocations: list[tuple[Batch, int]] = []
    for batch in batches:
        if remaining <= 0:
            break
        take = min(batch.qty_left, remaining)
        batch.qty_left -= take
        batch.save(update_fields=["qty_left"])
        InventoryMovement.objects.create(
            warehouse_id=warehouse_id,
            item_id=item_id,
            batch=batch,
            kind=kind,
            qty_delta=-take,
            balance_after=batch.qty_left,
            ref_type=ref_type,
            ref_id=str(ref_id) if ref_id != "" else "",
            actor=actor if (actor and actor.is_authenticated) else None,
        )
        allocations.append((batch, take))
        remaining -= take

    return allocations


def _sibling_batches(batch: Batch) -> list[Batch]:
    """Return target batch and all derived/sibling warehouse batches from the same lot."""
    q = Q(id=batch.id)
    if batch.receipt_id is not None:
        q |= Q(item_id=batch.item_id, expiry_date=batch.expiry_date, receipt_id=batch.receipt_id)
    if batch.origin_batch_id is not None:
        q |= Q(id=batch.origin_batch_id) | Q(origin_batch_id=batch.origin_batch_id)
    q |= Q(origin_batch_id=batch.id)
    return list(Batch.objects.select_for_update().filter(q).order_by("id"))


@transaction.atomic
def hold_batch(*, actor: User, batch_id: int, reason: str) -> dict[str, Any]:
    reason_clean = (reason or "").strip()
    if not reason_clean:
        msg = ("سبب إيقاف الدفعة مطلوب", "Hold reason is required")
        raise ApiException(code=ErrorCode.VALIDATION, message=msg, errors={"reason": msg})

    batch = Batch.objects.select_related("item", "held_by").filter(id=batch_id).first()
    if not batch:
        raise ApiException(code=ErrorCode.NOT_FOUND, status_code=http_status.HTTP_404_NOT_FOUND)

    own_wh = own_warehouse_id(actor)
    if own_wh is not None and batch.warehouse_id != own_wh:
        raise ApiException(code=ErrorCode.FORBIDDEN, status_code=http_status.HTTP_403_FORBIDDEN)

    if batch.held_at is not None:
        raise ApiException(
            code=ErrorCode.BAD_STATE,
            message=("هذه الدفعة موقوفة مسبقاً", "This batch is already on hold"),
        )

    now = datetime.now(tz=UTC)
    siblings = _sibling_batches(batch)
    for sib in siblings:
        sib.held_reason = reason_clean
        sib.held_by = actor
        sib.held_at = now
        sib.save(update_fields=["held_reason", "held_by", "held_at"])

    item_name = batch.item.name
    for target_role in (Role.MANAGER, Role.WAREHOUSE):
        notify_role(
            role=target_role,
            kind="batch_held",
            title_ar=f"إيقاف بيع الدفعة #{batch.id} ({item_name})",
            title_en=f"Batch #{batch.id} placed off-sale ({batch.item.name_en})",
            body_ar=reason_clean,
            body_en=reason_clean,
            ref_type="batch",
            ref_id=batch.id,
        )

    record_audit(
        actor=actor,
        action="batch.hold",
        entity_type="batch",
        entity_id=batch.id,
        summary=f"Held batch #{batch.id} ({len(siblings)} sibling(s)): {reason_clean}",
        after={"held_batch_ids": [s.id for s in siblings], "reason": reason_clean},
    )

    refreshed = Batch.objects.select_related("held_by").get(id=batch.id)
    return serialize_batch(refreshed, actor=actor)


@transaction.atomic
def release_batch(*, actor: User, batch_id: int) -> dict[str, Any]:
    batch = Batch.objects.select_related("item", "held_by").filter(id=batch_id).first()
    if not batch:
        raise ApiException(code=ErrorCode.NOT_FOUND, status_code=http_status.HTTP_404_NOT_FOUND)
    if batch.held_at is None:
        raise ApiException(
            code=ErrorCode.BAD_STATE,
            message=("هذه الدفعة ليست موقوفة حالياً", "This batch is not on hold"),
        )

    siblings = _sibling_batches(batch)
    for sib in siblings:
        sib.held_reason = ""
        sib.held_by = None
        sib.held_at = None
        sib.save(update_fields=["held_reason", "held_by", "held_at"])

    record_audit(
        actor=actor,
        action="batch.release",
        entity_type="batch",
        entity_id=batch.id,
        summary=f"Released batch #{batch.id} ({len(siblings)} sibling(s))",
        after={"released_batch_ids": [s.id for s in siblings]},
    )

    refreshed = Batch.objects.select_related("held_by").get(id=batch.id)
    return serialize_batch(refreshed, actor=actor)


def trace_batch_recipients(*, actor: User, batch_id: int) -> list[dict[str, Any]]:
    batch = Batch.objects.filter(id=batch_id).first()
    if not batch:
        raise ApiException(code=ErrorCode.NOT_FOUND, status_code=http_status.HTTP_404_NOT_FOUND)
    own_wh = own_warehouse_id(actor)
    if own_wh is not None and batch.warehouse_id != own_wh:
        raise ApiException(code=ErrorCode.FORBIDDEN, status_code=http_status.HTTP_403_FORBIDDEN)

    if not apps.is_installed("apps.invoices"):
        return []
    from apps.invoices.models import InvoiceBatchAllocation

    allocs = (
        InvoiceBatchAllocation.objects.filter(batch_id=batch_id)
        .select_related("invoice", "customer")
        .order_by("-invoice__delivered_at", "-id")
    )
    return [
        {
            "invoice_id": a.invoice_id,
            "invoice_number": a.invoice.number,
            "date": a.invoice.delivered_at.isoformat(),
            "customer": a.customer.name,
            "phone": a.customer.phone,
            "pieces": a.qty_pieces,
            "returned": a.returned_pieces,
        }
        for a in allocs
    ]
