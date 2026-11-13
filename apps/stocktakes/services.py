"""Stocktake services: open/count/close with warehouse locking & FEFO reconciliation."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from django.db import transaction
from django.db.models import Sum
from rest_framework import status as http_status

from apps.accounts.models import Role, User
from apps.accounts.permissions import own_warehouse_id
from apps.audit.services import record_audit
from apps.catalog.models import Item
from apps.common.exceptions import ApiException, ErrorCode
from apps.common.utils import baghdad_today
from apps.inventory.models import Batch, DamagedInventory, DamageSource, InventoryMovement, MovementKind
from apps.inventory.services import lock_and_deduct_fefo
from apps.notifications.services import notify_role
from apps.stocktakes.models import Stocktake, StocktakeLine, StocktakeStatus
from apps.warehouses.models import Warehouse


def serialize_stocktake(st: Stocktake) -> dict[str, Any]:
    return {
        "id": st.id,
        "number": st.number,
        "warehouse_id": st.warehouse_id,
        "category": st.category or None,
        "status": st.status,
        "created_at": st.created_at.isoformat(),
        "closed_at": st.closed_at.isoformat() if st.closed_at else None,
        "created_by": {"id": st.created_by_id, "name": st.created_by.name},
        "closed_by": {"id": st.closed_by_id, "name": st.closed_by.name} if st.closed_by else None,
        "lines": [
            {
                "id": ln.id,
                "item_id": ln.item_id,
                "system_qty": ln.system_qty,
                "counted_qty": ln.counted_qty,
                "damaged_qty": ln.damaged_qty,
                "reason": ln.reason,
            }
            for ln in st.lines.order_by("id")
        ],
    }


def list_stocktakes_for_actor(*, actor: User) -> list[dict[str, Any]]:
    qs = Stocktake.objects.select_related("created_by", "closed_by").prefetch_related("lines").order_by("-id")
    own_wh = own_warehouse_id(actor)
    if own_wh is not None:
        qs = qs.filter(warehouse_id=own_wh)
    return [serialize_stocktake(s) for s in qs]


def get_stocktake_for_actor(*, actor: User, stocktake_id: int) -> dict[str, Any]:
    st = Stocktake.objects.select_related("created_by", "closed_by").prefetch_related("lines").filter(id=stocktake_id).first()
    if not st:
        raise ApiException(code=ErrorCode.NOT_FOUND, status_code=http_status.HTTP_404_NOT_FOUND)
    own_wh = own_warehouse_id(actor)
    if own_wh is not None and st.warehouse_id != own_wh:
        raise ApiException(code=ErrorCode.FORBIDDEN, status_code=http_status.HTTP_403_FORBIDDEN)
    return serialize_stocktake(st)


@transaction.atomic
def create_stocktake(*, actor: User, data: dict[str, Any]) -> dict[str, Any]:
    wh_id = data.get("warehouse_id")
    wh = Warehouse.objects.filter(id=wh_id, active=True).first()
    if not wh:
        raise ApiException(code=ErrorCode.VALIDATION, message=("المخزن غير موجود", "Warehouse not found"))

    own_wh = own_warehouse_id(actor)
    if own_wh is not None and wh.id != own_wh:
        raise ApiException(code=ErrorCode.FORBIDDEN, status_code=http_status.HTTP_403_FORBIDDEN)

    category = str(data.get("category") or "").strip()
    open_existing = Stocktake.objects.filter(warehouse=wh, status=StocktakeStatus.OPEN)
    if not category:
        if open_existing.exists():
            raise ApiException(
                code=ErrorCode.BAD_STATE,
                message=("يوجد جرد مفتوح بالفعل في هذا المخزن", "An open stocktake already exists in this warehouse"),
            )
    else:
        if open_existing.filter(category="").exists() or open_existing.filter(category=category).exists():
            raise ApiException(
                code=ErrorCode.BAD_STATE,
                message=("هذه الفئة قيد الجرد حالياً في هذا المخزن", "This category/warehouse already has an open stocktake"),
            )

    today = baghdad_today()
    items_qs = Item.objects.filter(active=True).order_by("id")
    if category:
        items_qs = items_qs.filter(category=category)

    sys_map = {
        r["item_id"]: int(r["q"] or 0)
        for r in Batch.objects.filter(warehouse=wh, qty_left__gt=0, expiry_date__gte=today, held_at__isnull=True)
        .values("item_id")
        .annotate(q=Sum("qty_left"))
    }

    st = Stocktake.objects.create(
        number=f"TMP-{uuid4().hex[:10]}",
        warehouse=wh,
        category=category,
        status=StocktakeStatus.OPEN,
        created_by=actor,
    )
    st.number = f"STK-{st.id:04d}"
    st.save(update_fields=["number"])

    for it in items_qs:
        StocktakeLine.objects.create(
            stocktake=st,
            item=it,
            system_qty=sys_map.get(it.id, 0),
        )

    record_audit(
        actor=actor,
        action="stocktake.create",
        entity_type="stocktake",
        entity_id=st.id,
        summary=f"Opened stocktake {st.number} ({wh.code}, cat={category or 'ALL'})",
    )
    return get_stocktake_for_actor(actor=actor, stocktake_id=st.id)


@transaction.atomic
def update_stocktake(*, actor: User, stocktake_id: int, data: dict[str, Any]) -> dict[str, Any]:
    st = Stocktake.objects.select_for_update().prefetch_related("lines").filter(id=stocktake_id).first()
    if not st:
        raise ApiException(code=ErrorCode.NOT_FOUND, status_code=http_status.HTTP_404_NOT_FOUND)
    own_wh = own_warehouse_id(actor)
    if own_wh is not None and st.warehouse_id != own_wh:
        raise ApiException(code=ErrorCode.FORBIDDEN, status_code=http_status.HTTP_403_FORBIDDEN)
    if st.status != StocktakeStatus.OPEN:
        raise ApiException(code=ErrorCode.BAD_STATE, message=("الجرد مغلق مسبقاً", "Stocktake is already closed"))

    raw_lines = data.get("lines")
    if not isinstance(raw_lines, list):
        raise ApiException(code=ErrorCode.VALIDATION, message=("قائمة الجرد مطلوبة", "Lines array is required"))

    line_by_item = {ln.item_id: ln for ln in st.lines.all()}
    for entry in raw_lines:
        try:
            iid = int(entry["item_id"])
            ln = line_by_item.get(iid)
            if not ln:
                continue
            counted = entry.get("counted_qty")
            if counted is not None and counted != "":
                c_int = int(counted)
                if c_int < 0:
                    raise ValueError
                ln.counted_qty = c_int
            else:
                ln.counted_qty = None
            dmg = int(entry.get("damaged_qty") or 0)
            if dmg < 0:
                raise ValueError
            ln.damaged_qty = dmg
            ln.reason = str(entry.get("reason") or "").strip()
            ln.save(update_fields=["counted_qty", "damaged_qty", "reason"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ApiException(code=ErrorCode.VALIDATION, message=("قيم الجرد غير صالحة", "Invalid counted quantities")) from exc

    return get_stocktake_for_actor(actor=actor, stocktake_id=st.id)


@transaction.atomic
def close_stocktake(*, actor: User, stocktake_id: int) -> dict[str, Any]:
    st = (
        Stocktake.objects.select_for_update()
        .select_related("warehouse")
        .prefetch_related("lines__item")
        .filter(id=stocktake_id)
        .first()
    )
    if not st:
        raise ApiException(code=ErrorCode.NOT_FOUND, status_code=http_status.HTTP_404_NOT_FOUND)
    own_wh = own_warehouse_id(actor)
    if own_wh is not None and st.warehouse_id != own_wh:
        raise ApiException(code=ErrorCode.FORBIDDEN, status_code=http_status.HTTP_403_FORBIDDEN)
    if st.status != StocktakeStatus.OPEN:
        raise ApiException(code=ErrorCode.BAD_STATE, message=("الجرد مغلق مسبقاً", "Stocktake is already closed"))

    lines = list(st.lines.order_by("item_id"))
    for ln in lines:
        if ln.counted_qty is None:
            raise ApiException(
                code=ErrorCode.VALIDATION,
                message=("يجب إدخال العدد الفعلي لكل المواد قبل إغلاق الجرد", "Every item must have a counted quantity before closing"),
            )
        if (ln.counted_qty != ln.system_qty or ln.damaged_qty > 0) and not ln.reason.strip():
            raise ApiException(
                code=ErrorCode.VALIDATION,
                message=("يجب ذكر سبب لكل فرق في الجرد", "A reason is required for every count difference"),
            )

    today = baghdad_today()
    variances = 0
    for ln in lines:
        live_qty = int(
            Batch.objects.select_for_update()
            .filter(
                warehouse_id=st.warehouse_id,
                item_id=ln.item_id,
                qty_left__gt=0,
                expiry_date__gte=today,
                held_at__isnull=True,
            )
            .aggregate(q=Sum("qty_left"))["q"]
            or 0
        )
        diff = ln.counted_qty - live_qty
        if diff < 0:
            variances += 1
            shortage = abs(diff)
            allocs = lock_and_deduct_fefo(
                warehouse_id=st.warehouse_id,
                item_id=ln.item_id,
                qty_pieces=shortage,
                actor=actor,
                kind=MovementKind.STOCKTAKE_ADJUST,
                ref_type="stocktake",
                ref_id=st.id,
            )
            dmg_pieces = ln.damaged_qty if ln.damaged_qty > 0 else shortage
            DamagedInventory.objects.create(
                warehouse_id=st.warehouse_id,
                item_id=ln.item_id,
                batch=allocs[0][0] if allocs else None,
                pieces=dmg_pieces,
                source=DamageSource.STOCKTAKE,
                ref=st.number,
                reason=ln.reason,
                recorded_by=actor,
            )
        elif diff > 0:
            variances += 1
            adj_batch = Batch.objects.create(
                item_id=ln.item_id,
                warehouse_id=st.warehouse_id,
                qty_received=diff,
                qty_left=diff,
                purchase_price=ln.item.avg_cost,
                expiry_date=today + timedelta(days=180),
            )
            InventoryMovement.objects.create(
                warehouse_id=st.warehouse_id,
                item_id=ln.item_id,
                batch=adj_batch,
                kind=MovementKind.STOCKTAKE_ADJUST,
                qty_delta=diff,
                balance_after=diff,
                ref_type="stocktake",
                ref_id=str(st.id),
                actor=actor,
            )

    st.status = StocktakeStatus.CLOSED
    st.closed_by = actor
    st.closed_at = datetime.now(tz=UTC)
    st.save(update_fields=["status", "closed_by", "closed_at"])

    if variances > 0:
        notify_role(
            role=Role.MANAGER,
            kind="stocktake_closed",
            title_ar=f"إغلاق جرد {st.number} مع {variances} فرق",
            title_en=f"Stocktake {st.number} closed with {variances} variance(s)",
            ref_type="stocktake",
            ref_id=st.id,
        )

    record_audit(
        actor=actor,
        action="stocktake.close",
        entity_type="stocktake",
        entity_id=st.id,
        summary=f"Closed stocktake {st.number} ({variances} variance(s))",
    )
    return get_stocktake_for_actor(actor=actor, stocktake_id=st.id)
