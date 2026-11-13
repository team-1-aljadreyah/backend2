"""Receiving goods services: validation, atomic batch creation, weighted avg_cost."""
from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from uuid import uuid4

from django.db import transaction
from django.db.models import Sum
from rest_framework import status as http_status

from apps.accounts.models import User
from apps.accounts.permissions import own_warehouse_id, sees_cost
from apps.audit.services import record_audit
from apps.catalog.models import Item
from apps.common.exceptions import ApiException, ErrorCode
from apps.common.utils import baghdad_today
from apps.inventory.models import Batch, InventoryMovement, MovementKind
from apps.inventory.services import is_count_locked
from apps.receipts.models import Receipt, ReceiptLine, UnitChoice
from apps.warehouses.models import Warehouse


def serialize_receipt(receipt: Receipt, *, actor: User) -> dict[str, Any]:
    can_cost = sees_cost(actor)
    lines_out = []
    for ln in receipt.lines.select_related("created_batch").order_by("id"):
        cb = getattr(ln, "created_batch", None)
        lines_out.append(
            {
                "item_id": ln.item_id,
                "batch_id": cb.id if cb else None,
                "unit": ln.unit,
                "qty": ln.qty,
                "pieces": ln.qty_pieces,
                "expiry_date": ln.expiry_date.isoformat(),
                "purchase_price": ln.purchase_price if can_cost else 0,
            }
        )
    out: dict[str, Any] = {
        "id": receipt.id,
        "number": receipt.number,
        "warehouse_id": receipt.warehouse_id,
        "supplier": receipt.supplier,
        "created_at": receipt.created_at.isoformat(),
        "created_by": {
            "id": receipt.created_by_id,
            "name": receipt.created_by.name,
        },
        "lines": lines_out,
    }
    if receipt.supply_order_id is not None:
        out["supply_order_id"] = receipt.supply_order_id
    if can_cost:
        out["total_cost"] = receipt.total_cost
    return out


def list_receipts_for_actor(*, actor: User) -> list[dict[str, Any]]:
    qs = Receipt.objects.select_related("created_by").prefetch_related("lines__created_batch").order_by("-id")
    own_wh = own_warehouse_id(actor)
    if own_wh is not None:
        qs = qs.filter(warehouse_id=own_wh)
    return [serialize_receipt(r, actor=actor) for r in qs]


def get_receipt_for_actor(*, actor: User, receipt_id: int) -> dict[str, Any]:
    r = Receipt.objects.select_related("created_by").prefetch_related("lines__created_batch").filter(id=receipt_id).first()
    if not r:
        raise ApiException(code=ErrorCode.NOT_FOUND, status_code=http_status.HTTP_404_NOT_FOUND)
    own_wh = own_warehouse_id(actor)
    if own_wh is not None and r.warehouse_id != own_wh:
        raise ApiException(code=ErrorCode.FORBIDDEN, status_code=http_status.HTTP_403_FORBIDDEN)
    return serialize_receipt(r, actor=actor)


@transaction.atomic
def create_receipt(*, actor: User, data: dict[str, Any]) -> dict[str, Any]:
    client_ref = (str(data.get("client_ref") or "")).strip() or None
    if client_ref:
        existing = (
            Receipt.objects.select_related("created_by")
            .prefetch_related("lines__created_batch")
            .filter(client_ref=client_ref)
            .first()
        )
        if existing:
            return serialize_receipt(existing, actor=actor)

    wh_id = data.get("warehouse_id")
    wh = Warehouse.objects.filter(id=wh_id, active=True).first()
    if not wh:
        raise ApiException(
            code=ErrorCode.VALIDATION,
            message=("المخزن المحدد غير موجود", "Selected warehouse not found"),
            errors={"warehouse_id": ("المخزن غير موجود", "Warehouse not found")},
        )
    own_wh = own_warehouse_id(actor)
    if own_wh is not None and wh.id != own_wh:
        raise ApiException(code=ErrorCode.FORBIDDEN, status_code=http_status.HTTP_403_FORBIDDEN)

    raw_so_id = data.get("supply_order_id")
    so_id: int | None = None
    if raw_so_id not in (None, "", 0):
        try:
            so_id = int(raw_so_id)
            if so_id <= 0:
                raise ValueError
        except (TypeError, ValueError) as exc:
            msg = ("رقم طلب التوريد غير صالح", "Invalid supply_order_id")
            raise ApiException(code=ErrorCode.VALIDATION, message=msg, errors={"supply_order_id": msg}) from exc

    supplier = str(data.get("supplier") or "").strip()
    raw_lines = data.get("lines")
    if not isinstance(raw_lines, list) or not raw_lines:
        raise ApiException(
            code=ErrorCode.VALIDATION,
            message=("أضف مادة واحدة على الأقل للاستلام", "At least one receipt line is required"),
        )

    today = baghdad_today()
    errors: dict[str, tuple[str, str]] = {}
    item_ids: set[int] = set()
    for idx, ln in enumerate(raw_lines):
        try:
            iid = int(ln.get("item_id"))
            item_ids.add(iid)
        except (TypeError, ValueError):
            errors[f"lines.{idx}.item_id"] = ("اختر المادة", "Select an item")

    # Lock Items in deterministic ID order so concurrent receipts serialize weighted average cost safely
    items_map = {it.id: it for it in Item.objects.select_for_update().filter(id__in=sorted(item_ids)).order_by("id")}
    parsed_lines: list[dict[str, Any]] = []

    for idx, ln in enumerate(raw_lines):
        iid = ln.get("item_id")
        try:
            iid_int = int(iid)
        except (TypeError, ValueError):
            iid_int = -1
        item = items_map.get(iid_int)
        if not item:
            errors[f"lines.{idx}.item_id"] = ("المادة غير موجودة", "Item not found")
            continue

        if is_count_locked(wh.id, item.category):
            raise ApiException(
                code=ErrorCode.BAD_STATE,
                message=("المخزن قيد الجرد حالياً لهذه الفئة", "Stocktake is currently open for this warehouse/category"),
            )

        unit = ln.get("unit")
        if unit not in (UnitChoice.CARTON, UnitChoice.PIECE):
            errors[f"lines.{idx}.unit"] = ("وحدة غير صالحة", "Invalid unit")
            continue

        try:
            qty = int(ln.get("qty") or 0)
            if qty <= 0:
                raise ValueError
        except (TypeError, ValueError):
            errors[f"lines.{idx}.qty"] = ("الكمية يجب أن تكون أكبر من صفر", "Quantity must be positive")
            continue

        try:
            price = int(ln.get("purchase_price"))
            if price < 0:
                raise ValueError
        except (TypeError, ValueError):
            errors[f"lines.{idx}.purchase_price"] = ("سعر الشراء غير صحيح", "Invalid purchase price")
            continue

        raw_exp = str(ln.get("expiry_date") or "").strip()
        try:
            exp_date = date.fromisoformat(raw_exp)
        except ValueError:
            errors[f"lines.{idx}.expiry_date"] = ("تاريخ الصلاحية غير صحيح", "Invalid expiry date")
            continue
        if exp_date < today:
            errors[f"lines.{idx}.expiry_date"] = ("لا يمكن استلام بضاعة منتهية الصلاحية", "Cannot receive already-expired stock")
            continue

        pieces = qty * item.pieces_per_carton if unit == UnitChoice.CARTON else qty
        parsed_lines.append(
            {
                "item": item,
                "unit": unit,
                "qty": qty,
                "pieces": pieces,
                "purchase_price": price,
                "expiry_date": exp_date,
            }
        )

    if errors:
        raise ApiException(code=ErrorCode.VALIDATION, message=next(iter(errors.values())), errors=errors)

    # Current non-expired physical stock per item for moving average cost
    existing_qty_map = {
        r["item_id"]: int(r["q"] or 0)
        for r in Batch.objects.filter(item_id__in=items_map.keys(), qty_left__gt=0, expiry_date__gte=today)
        .values("item_id")
        .annotate(q=Sum("qty_left"))
    }

    total_cost = sum(pl["pieces"] * pl["purchase_price"] for pl in parsed_lines)
    receipt = Receipt.objects.create(
        number=f"TMP-{uuid4().hex[:10]}",
        warehouse=wh,
        supply_order_id=so_id,
        supplier=supplier,
        total_cost=total_cost,
        client_ref=client_ref,
        created_by=actor,
    )
    receipt.number = f"RCV-{receipt.id:04d}"
    receipt.save(update_fields=["number"])

    if so_id is not None:
        from apps.supply_orders.services import apply_receipt_to_supply_order

        apply_receipt_to_supply_order(
            supply_order_id=so_id,
            warehouse_id=wh.id,
            receipt=receipt,
            parsed_lines=parsed_lines,
            actor=actor,
        )

    for pl in parsed_lines:
        item: Item = pl["item"]
        pieces: int = pl["pieces"]
        pp: int = pl["purchase_price"]
        old_qty = existing_qty_map.get(item.id, 0)
        denom = old_qty + pieces
        if denom > 0:
            avg_dec = (Decimal(old_qty) * Decimal(item.avg_cost) + Decimal(pieces) * Decimal(pp)) / Decimal(denom)
            item.avg_cost = int(avg_dec.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
            item.save(update_fields=["avg_cost"])
        existing_qty_map[item.id] = denom

        rline = ReceiptLine.objects.create(
            receipt=receipt,
            item=item,
            unit=pl["unit"],
            qty=pl["qty"],
            qty_pieces=pieces,
            purchase_price=pp,
            expiry_date=pl["expiry_date"],
        )
        batch = Batch.objects.create(
            item=item,
            warehouse=wh,
            receipt=receipt,
            receipt_line=rline,
            qty_received=pieces,
            qty_left=pieces,
            purchase_price=pp,
            expiry_date=pl["expiry_date"],
        )
        InventoryMovement.objects.create(
            warehouse=wh,
            item=item,
            batch=batch,
            kind=MovementKind.RECEIPT,
            qty_delta=pieces,
            balance_after=pieces,
            ref_type="receipt",
            ref_id=str(receipt.id),
            actor=actor,
        )

    record_audit(
        actor=actor,
        action="receipt.create",
        entity_type="receipt",
        entity_id=receipt.id,
        summary=f"Created receipt {receipt.number} ({len(parsed_lines)} line(s))",
        after={"number": receipt.number, "warehouse_id": wh.id, "total_cost": total_cost},
    )
    from apps.catalog.features_services import trigger_back_in_stock_alerts

    trigger_back_in_stock_alerts([pl["item"].id for pl in parsed_lines])
    return get_receipt_for_actor(actor=actor, receipt_id=receipt.id)
