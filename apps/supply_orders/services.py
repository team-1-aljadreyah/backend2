"""Supply orders services: list, detail, create, cancel, and receipt reconciliation."""
from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any
from uuid import uuid4

from django.db import transaction
from rest_framework import status as http_status

from apps.accounts.models import Role, User
from apps.accounts.permissions import own_warehouse_id
from apps.audit.services import record_audit
from apps.catalog.models import Item
from apps.common.exceptions import ApiException, ErrorCode
from apps.common.utils import baghdad_today
from apps.notifications.services import notify_role, notify_user
from apps.supply_orders.models import SupplyOrder, SupplyOrderLine, SupplyStatus
from apps.warehouses.models import Warehouse


def _num_qty(val: Decimal | int | float) -> int | float:
    d = Decimal(str(val))
    if d == d.to_integral_value():
        return int(d)
    return float(d.quantize(Decimal("0.01")))


def serialize_supply_order(order: SupplyOrder) -> dict[str, Any]:
    receipt_ids = list(order.receipts.order_by("id").values_list("id", flat=True))
    out: dict[str, Any] = {
        "id": order.id,
        "number": order.number,
        "warehouse_id": order.warehouse_id,
        "supplier": order.supplier,
        "lines": [
            {
                "item_id": ln.item_id,
                "unit": ln.unit,
                "qty": ln.qty,
                "price": ln.purchase_price,
                "received_qty": _num_qty(ln.received_qty),
            }
            for ln in order.lines.order_by("id")
        ],
        "total": order.total,
        "status": order.status,
        "receipt_ids": receipt_ids,
        "created_at": order.created_at.isoformat(),
        "created_by": {
            "id": order.created_by_id,
            "name": order.created_by.name,
            "role": order.created_by.role,
        },
    }
    if order.supplier_phone:
        out["supplier_phone"] = order.supplier_phone
    if order.expected_date:
        out["expected_date"] = order.expected_date.isoformat()
    if order.note:
        out["note"] = order.note
    if order.cancel_reason:
        out["cancel_reason"] = order.cancel_reason
    if order.closed_short:
        out["closed_short"] = order.closed_short
    return out


def list_supply_orders_for_actor(*, actor: User) -> list[dict[str, Any]]:
    qs = (
        SupplyOrder.objects.select_related("created_by")
        .prefetch_related("lines", "receipts")
        .order_by("-id")
    )
    own_wh = own_warehouse_id(actor)
    if own_wh is not None:
        qs = qs.filter(warehouse_id=own_wh)
    return [serialize_supply_order(o) for o in qs]


def get_supply_order_for_actor(*, actor: User, supply_order_id: int) -> dict[str, Any]:
    order = (
        SupplyOrder.objects.select_related("created_by")
        .prefetch_related("lines", "receipts")
        .filter(id=supply_order_id)
        .first()
    )
    if not order:
        raise ApiException(code=ErrorCode.NOT_FOUND, status_code=http_status.HTTP_404_NOT_FOUND)
    own_wh = own_warehouse_id(actor)
    if own_wh is not None and order.warehouse_id != own_wh:
        raise ApiException(code=ErrorCode.NOT_FOUND, status_code=http_status.HTTP_404_NOT_FOUND)
    return serialize_supply_order(order)


@transaction.atomic
def create_supply_order(*, actor: User, data: dict[str, Any]) -> dict[str, Any]:
    wh_id = data.get("warehouse_id")
    wh = Warehouse.objects.filter(id=wh_id, active=True).first()
    if not wh:
        msg = ("اختر المخزن", "Choose the warehouse")
        raise ApiException(code=ErrorCode.VALIDATION, message=msg, errors={"warehouse_id": msg})

    errors: dict[str, tuple[str, str]] = {}
    supplier = str(data.get("supplier") or "").strip()
    if not supplier:
        errors["supplier"] = ("اكتب اسم المورّد", "Write the supplier's name")

    raw_lines = data.get("lines")
    valid_raw: list[dict[str, Any]] = []
    if isinstance(raw_lines, list):
        for ln in raw_lines:
            if not isinstance(ln, dict) or not ln.get("item_id"):
                continue
            try:
                q_probe = int(ln.get("qty") or 0)
            except (TypeError, ValueError):
                q_probe = 1
            if q_probe > 0:
                valid_raw.append(ln)
    if not valid_raw:
        errors["lines"] = ("أضف مادة واحدة على الأقل", "Add at least one item")

    exp_date: date | None = None
    raw_exp = str(data.get("expected_date") or "").strip()
    if raw_exp:
        try:
            exp_date = date.fromisoformat(raw_exp)
            if exp_date < baghdad_today():
                errors["expected_date"] = ("تاريخ في الماضي", "A date in the past")
        except ValueError:
            errors["expected_date"] = ("تاريخ غير صالح", "Invalid date")

    item_ids: set[int] = set()
    for ln in valid_raw:
        try:
            iid_val = int(ln["item_id"])
            if iid_val > 0:
                item_ids.add(iid_val)
        except (TypeError, ValueError):
            continue
    items_map = {it.id: it for it in Item.objects.filter(id__in=item_ids)}
    parsed_lines: list[dict[str, Any]] = []

    for idx, ln in enumerate(valid_raw):
        try:
            iid = int(ln.get("item_id"))
        except (TypeError, ValueError):
            iid = -1
        if iid not in items_map:
            errors[f"lines.{idx}.item_id"] = ("المادة غير موجودة", "Item not found")
            continue

        unit = ln.get("unit")
        if unit not in ("carton", "piece"):
            errors[f"lines.{idx}.unit"] = ("وحدة غير صالحة", "Invalid unit")
            continue

        qty_raw = ln.get("qty")
        if isinstance(qty_raw, bool) or not isinstance(qty_raw, int) or qty_raw <= 0:
            errors[f"lines.{idx}.qty"] = ("عدد صحيح موجب", "Positive integer quantity")
            continue

        try:
            raw_price = ln.get("price")
            if isinstance(raw_price, bool) or raw_price in (None, ""):
                raise ValueError
            price_dec = Decimal(str(raw_price))
            if not price_dec.is_finite():
                raise InvalidOperation
            price = int(price_dec.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
            if price <= 0:
                raise ValueError
        except (InvalidOperation, TypeError, ValueError):
            errors[f"lines.{idx}.price"] = ("سعر الشراء مطلوب", "Purchase price is required")
            continue

        parsed_lines.append({"item_id": iid, "unit": unit, "qty": qty_raw, "price": price})

    if errors:
        raise ApiException(code=ErrorCode.VALIDATION, message=next(iter(errors.values())), errors=errors)

    total = sum(pl["price"] * pl["qty"] for pl in parsed_lines)
    order = SupplyOrder.objects.create(
        number=f"TMP-{uuid4().hex[:10]}",
        warehouse=wh,
        supplier=supplier,
        supplier_phone=str(data.get("supplier_phone") or "").strip()[:32],
        total=total,
        status=SupplyStatus.SENT,
        expected_date=exp_date,
        note=str(data.get("note") or "").strip(),
        created_by=actor,
    )
    order.number = f"SUP-{order.id:04d}"
    order.save(update_fields=["number"])

    SupplyOrderLine.objects.bulk_create(
        [
            SupplyOrderLine(
                supply_order=order,
                item_id=pl["item_id"],
                unit=pl["unit"],
                qty=pl["qty"],
                received_qty=Decimal("0"),
                purchase_price=pl["price"],
            )
            for pl in parsed_lines
        ]
    )

    for keeper_id in User.objects.filter(role=Role.WAREHOUSE, active=True, warehouse_id=wh.id).values_list("id", flat=True):
        notify_user(
            user_id=keeper_id,
            kind="supply_order",
            title_ar=f"طلب توريد جديد: {order.number}",
            title_en=f"New supply order: {order.number}",
            body_ar=f"{order.supplier} · {len(parsed_lines)} مواد",
            body_en=f"{order.supplier} · {len(parsed_lines)} items",
            ref_type="supply_order",
            ref_id=order.id,
        )

    record_audit(
        actor=actor,
        action="supply_order.create",
        entity_type="supply_order",
        entity_id=order.id,
        summary=f"Supply order {order.number} to {wh.name_en}",
        after={"number": order.number, "warehouse_id": wh.id, "total": total},
    )
    return get_supply_order_for_actor(actor=actor, supply_order_id=order.id)


@transaction.atomic
def cancel_supply_order(*, actor: User, supply_order_id: int, reason: str) -> dict[str, Any]:
    order = SupplyOrder.objects.select_for_update().filter(id=supply_order_id).first()
    if not order:
        raise ApiException(code=ErrorCode.NOT_FOUND, status_code=http_status.HTTP_404_NOT_FOUND)
    if order.status != SupplyStatus.SENT:
        raise ApiException(
            code=ErrorCode.BAD_STATE,
            message=("وصل شيء من هذا الطلب: لا يُلغى", "Part of it already arrived or order is closed"),
        )
    reason_clean = str(reason or "").strip()
    if not reason_clean:
        msg = ("اكتب السبب", "Write the reason")
        raise ApiException(code=ErrorCode.VALIDATION, message=msg, errors={"reason": msg})

    order.status = SupplyStatus.CANCELLED
    order.cancel_reason = reason_clean
    order.cancelled_by = actor
    order.cancelled_at = datetime.now(tz=UTC)
    order.save(update_fields=["status", "cancel_reason", "cancelled_by", "cancelled_at", "updated_at"])

    for keeper_id in User.objects.filter(role=Role.WAREHOUSE, active=True, warehouse_id=order.warehouse_id).values_list("id", flat=True):
        notify_user(
            user_id=keeper_id,
            kind="supply_order",
            title_ar=f"أُلغي طلب التوريد {order.number}",
            title_en=f"{order.number} was cancelled",
            body_ar=reason_clean,
            body_en=reason_clean,
            ref_type="supply_order",
            ref_id=order.id,
        )

    record_audit(
        actor=actor,
        action="supply_order.cancel",
        entity_type="supply_order",
        entity_id=order.id,
        summary=f"Cancelled {order.number}: {reason_clean}",
    )
    return get_supply_order_for_actor(actor=actor, supply_order_id=order.id)


def apply_receipt_to_supply_order(*, supply_order_id: int, warehouse_id: int, receipt, parsed_lines: list[dict[str, Any]], actor: User) -> SupplyOrder:
    """Lock a supply order and increment received quantities from a receipt inside an atomic transaction."""
    order = SupplyOrder.objects.select_for_update().filter(id=supply_order_id).first()
    if not order:
        raise ApiException(
            code=ErrorCode.NOT_FOUND,
            message=("طلب التوريد غير موجود", "Supply order not found"),
        )
    if order.status in (SupplyStatus.CANCELLED, SupplyStatus.RECEIVED):
        raise ApiException(
            code=ErrorCode.BAD_STATE,
            message=("طلب التوريد مغلق", "This supply order is closed"),
        )
    if order.warehouse_id != warehouse_id:
        msg = ("طلب التوريد لمخزن آخر", "The supply order is for another warehouse")
        raise ApiException(code=ErrorCode.VALIDATION, message=msg, errors={"supply_order_id": msg})

    slines = list(SupplyOrderLine.objects.select_for_update().filter(supply_order=order).order_by("id"))
    for idx, pl in enumerate(parsed_lines):
        item: Item = pl["item"]
        unit = pl["unit"]
        qty = pl["qty"]
        pieces = pl["pieces"]
        target = next((sln for sln in slines if sln.item_id == item.id and sln.unit == unit), None)
        if not target:
            target = next((sln for sln in slines if sln.item_id == item.id), None)
        if not target:
            msg = ("المادة غير مدرجة في طلب التوريد", "Item is not included in the selected supply order")
            raise ApiException(code=ErrorCode.VALIDATION, message=msg, errors={f"lines.{idx}.item_id": msg})
        if target.unit == unit:
            in_unit = Decimal(qty)
        elif target.unit == "carton":
            in_unit = (Decimal(pieces) / Decimal(item.pieces_per_carton)).quantize(Decimal("0.01"))
        else:
            in_unit = Decimal(pieces)
        new_received = (Decimal(target.received_qty) + in_unit).quantize(Decimal("0.01"))
        if new_received > Decimal(target.qty):
            msg = (
                "الكمية المستلمة تتجاوز الكمية المطلوبة في طلب التوريد",
                "Received quantity exceeds remaining quantity in the supply order",
            )
            raise ApiException(code=ErrorCode.VALIDATION, message=msg, errors={f"lines.{idx}.qty": msg})
        target.received_qty = new_received
        target.save(update_fields=["received_qty"])

    complete = all(Decimal(sln.received_qty) >= Decimal(sln.qty) for sln in slines)
    order.status = SupplyStatus.RECEIVED if complete else SupplyStatus.PARTIAL
    order.save(update_fields=["status", "updated_at"])

    if actor.role != Role.ACCOUNTANT:
        for role in (Role.ACCOUNTANT, Role.MANAGER):
            notify_role(
                role=role,
                kind="supply_order",
                title_ar=f"وصل طلب التوريد {order.number} كاملاً" if complete else f"وصل جزء من طلب التوريد {order.number}",
                title_en=f"{order.number} arrived in full" if complete else f"Part of {order.number} arrived",
                body_ar=f"{receipt.number} · {actor.name}",
                body_en=receipt.number,
                ref_type="supply_order",
                ref_id=order.id,
            )
    return order
