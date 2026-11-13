"""Inter-warehouse transfer services with FEFO batch lineage preservation."""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from django.db import transaction
from django.db.models import Q
from rest_framework import status as http_status

from apps.accounts.models import Role, User
from apps.accounts.permissions import own_warehouse_id
from apps.audit.services import record_audit
from apps.catalog.models import Item
from apps.common.exceptions import ApiException, ErrorCode
from apps.inventory.models import Batch, InventoryMovement, MovementKind
from apps.inventory.services import is_count_locked, lock_and_deduct_fefo, shelf_available
from apps.notifications.services import notify_role
from apps.transfers.models import Transfer, TransferBatchAllocation, TransferLine, TransferStatus
from apps.warehouses.models import Warehouse


def serialize_transfer(t: Transfer) -> dict[str, Any]:
    return {
        "id": t.id,
        "number": t.number,
        "from_warehouse_id": t.from_warehouse_id,
        "to_warehouse_id": t.to_warehouse_id,
        "status": t.status,
        "reason": t.reason,
        "note": t.note or None,
        "diff_reason": t.diff_reason or None,
        "client_ref": t.client_ref or None,
        "created_at": t.created_at.isoformat(),
        "received_at": t.received_at.isoformat() if t.received_at else None,
        "reviewed_at": t.reviewed_at.isoformat() if t.reviewed_at else None,
        "created_by": {"id": t.created_by_id, "name": t.created_by.name},
        "received_by": {"id": t.received_by_id, "name": t.received_by.name} if t.received_by else None,
        "reviewed_by": {"id": t.reviewed_by_id, "name": t.reviewed_by.name} if t.reviewed_by else None,
        "lines": [
            {
                "id": ln.id,
                "item_id": ln.item_id,
                "pieces": ln.pieces,
                "received": ln.received_pieces,
            }
            for ln in t.lines.order_by("id")
        ],
    }


def list_transfers_for_actor(*, actor: User) -> list[dict[str, Any]]:
    qs = Transfer.objects.select_related("created_by", "received_by", "reviewed_by").prefetch_related("lines").order_by("-id")
    own_wh = own_warehouse_id(actor)
    if own_wh is not None:
        qs = qs.filter(Q(from_warehouse_id=own_wh) | Q(to_warehouse_id=own_wh))
    return [serialize_transfer(t) for t in qs]


@transaction.atomic
def create_transfer(*, actor: User, data: dict[str, Any]) -> dict[str, Any]:
    client_ref = (str(data.get("client_ref") or "")).strip() or None
    if client_ref:
        existing = Transfer.objects.filter(client_ref=client_ref).select_related("created_by", "received_by", "reviewed_by").prefetch_related("lines").first()
        if existing:
            return serialize_transfer(existing)

    from_wh_id = data.get("from_warehouse_id")
    to_wh_id = data.get("to_warehouse_id")
    from_wh = Warehouse.objects.filter(id=from_wh_id, active=True).first()
    to_wh = Warehouse.objects.filter(id=to_wh_id, active=True).first()
    if not from_wh or not to_wh or from_wh.id == to_wh.id:
        raise ApiException(
            code=ErrorCode.VALIDATION,
            message=("اختر مخزني إرسال واستلام مختلفين", "Select two distinct valid warehouses"),
        )

    own_wh = own_warehouse_id(actor)
    if own_wh is not None and from_wh.id != own_wh:
        raise ApiException(code=ErrorCode.FORBIDDEN, status_code=http_status.HTTP_403_FORBIDDEN)

    raw_lines = data.get("lines")
    if not isinstance(raw_lines, list) or not raw_lines:
        raise ApiException(
            code=ErrorCode.VALIDATION,
            message=("أضف مادة واحدة على الأقل للتحويل", "At least one transfer line is required"),
        )

    aggregated: dict[int, int] = {}
    for ln in raw_lines:
        try:
            iid = int(ln["item_id"])
            item = Item.objects.get(id=iid)
            if "pieces" in ln and ln["pieces"] is not None:
                pcs = int(ln["pieces"])
            else:
                unit = ln.get("unit", "piece")
                qty = int(ln["qty"])
                pcs = qty * item.pieces_per_carton if unit == "carton" else qty
            if pcs <= 0:
                raise ValueError
        except (KeyError, TypeError, ValueError, Item.DoesNotExist) as exc:
            raise ApiException(code=ErrorCode.VALIDATION, message=("تفاصيل المادة أو الكمية غير صحيحة", "Invalid transfer line item or qty")) from exc
        aggregated[iid] = aggregated.get(iid, 0) + pcs

    items_map = {
        it.id: it
        for it in Item.objects.select_for_update().filter(id__in=sorted(aggregated.keys())).order_by("id")
    }
    for iid in aggregated:
        cat = items_map[iid].category
        if is_count_locked(from_wh.id, cat) or is_count_locked(to_wh.id, cat):
            raise ApiException(
                code=ErrorCode.BAD_STATE,
                message=("أحد المخزنين قيد الجرد حالياً لهذه الفئة", "Warehouse is locked by an open stocktake for this category"),
            )

    transfer = Transfer.objects.create(
        number=f"TMP-{uuid4().hex[:10]}",
        from_warehouse=from_wh,
        to_warehouse=to_wh,
        status=TransferStatus.SENT,
        reason=str(data.get("reason") or "").strip(),
        note=str(data.get("note") or "").strip(),
        client_ref=client_ref,
        created_by=actor,
    )
    transfer.number = f"TRF-{transfer.id:04d}"
    transfer.save(update_fields=["number"])

    for iid in sorted(aggregated.keys()):
        pcs = aggregated[iid]
        if shelf_available(from_wh.id, iid) < pcs:
            raise ApiException(
                code=ErrorCode.OUT_OF_STOCK,
                message=("الكمية المتوفرة غير كافية (بعد استبعاد المحجوز)", "Insufficient unreserved sellable stock for transfer"),
                status_code=http_status.HTTP_409_CONFLICT,
            )
        tline = TransferLine.objects.create(transfer=transfer, item_id=iid, pieces=pcs)
        allocs = lock_and_deduct_fefo(
            warehouse_id=from_wh.id,
            item_id=iid,
            qty_pieces=pcs,
            actor=actor,
            kind=MovementKind.TRANSFER_OUT,
            ref_type="transfer",
            ref_id=transfer.id,
        )
        for batch, take in allocs:
            TransferBatchAllocation.objects.create(
                transfer_line=tline,
                source_batch=batch,
                qty=take,
                expiry_date=batch.expiry_date,
                purchase_price=batch.purchase_price,
                receipt_id=batch.receipt_id,
            )

    notify_role(
        role=Role.WAREHOUSE,
        kind="transfer_sent",
        title_ar=f"تحويل وارد {transfer.number} إلى {to_wh.name}",
        title_en=f"Incoming transfer {transfer.number} to {to_wh.name_en}",
        ref_type="transfer",
        ref_id=transfer.id,
    )
    record_audit(
        actor=actor,
        action="transfer.create",
        entity_type="transfer",
        entity_id=transfer.id,
        summary=f"Created transfer {transfer.number} ({from_wh.code} -> {to_wh.code})",
        after={"number": transfer.number, "lines": [{"item_id": k, "pieces": v} for k, v in aggregated.items()]},
    )
    return serialize_transfer(Transfer.objects.select_related("created_by", "received_by", "reviewed_by").prefetch_related("lines").get(id=transfer.id))


@transaction.atomic
def receive_transfer(*, actor: User, transfer_id: int, data: dict[str, Any]) -> dict[str, Any]:
    transfer = (
        Transfer.objects.select_for_update()
        .select_related("from_warehouse", "to_warehouse", "created_by")
        .prefetch_related("lines__item", "lines__batch_allocations")
        .filter(id=transfer_id)
        .first()
    )
    if not transfer:
        raise ApiException(code=ErrorCode.NOT_FOUND, status_code=http_status.HTTP_404_NOT_FOUND)

    own_wh = own_warehouse_id(actor)
    if own_wh is not None and transfer.to_warehouse_id != own_wh:
        raise ApiException(code=ErrorCode.FORBIDDEN, status_code=http_status.HTTP_403_FORBIDDEN)

    if transfer.status != TransferStatus.SENT:
        raise ApiException(
            code=ErrorCode.BAD_STATE,
            message=("تم استلام هذا التحويل مسبقاً", "This transfer has already been received"),
        )

    received_map = data.get("received")
    if not isinstance(received_map, dict):
        raise ApiException(code=ErrorCode.VALIDATION, message=("بيانات الاستلام غير صالحة", "Invalid received quantities payload"))

    reason = str(data.get("reason") or "").strip()
    has_discrepancy = False
    line_qtys: list[tuple[TransferLine, int]] = []

    for ln in transfer.lines.order_by("id"):
        if is_count_locked(transfer.to_warehouse_id, ln.item.category):
            raise ApiException(
                code=ErrorCode.BAD_STATE,
                message=("مخزن الاستلام قيد الجرد حالياً لهذه الفئة", "Destination warehouse locked by open stocktake"),
            )
        raw_val = received_map.get(str(ln.item_id), received_map.get(ln.item_id, received_map.get(str(ln.id), ln.pieces)))
        try:
            recv_qty = int(raw_val)
            if recv_qty < 0 or recv_qty > ln.pieces:
                raise ValueError
        except (TypeError, ValueError) as exc:
            raise ApiException(
                code=ErrorCode.VALIDATION,
                message=("الكمية المستلمة يجب أن تكون بين 0 والكمية المرسلة", "Received qty must be between 0 and sent qty"),
            ) from exc
        if recv_qty != ln.pieces:
            has_discrepancy = True
        line_qtys.append((ln, recv_qty))

    if has_discrepancy and not reason:
        msg = ("يرجى كتابة سبب النقص في الاستلام", "A discrepancy reason is required when received differs from sent")
        raise ApiException(code=ErrorCode.VALIDATION, message=msg, errors={"reason": msg})

    for ln, recv_qty in line_qtys:
        ln.received_pieces = recv_qty
        ln.save(update_fields=["received_pieces"])

        remaining_to_create = recv_qty
        for alloc in ln.batch_allocations.order_by("expiry_date", "id"):
            if remaining_to_create <= 0:
                break
            take = min(alloc.qty, remaining_to_create)
            dest_batch = Batch.objects.create(
                item_id=ln.item_id,
                warehouse_id=transfer.to_warehouse_id,
                receipt_id=alloc.receipt_id,
                origin_batch_id=alloc.source_batch_id,
                qty_received=take,
                qty_left=take,
                purchase_price=alloc.purchase_price,
                expiry_date=alloc.expiry_date,
            )
            InventoryMovement.objects.create(
                warehouse_id=transfer.to_warehouse_id,
                item_id=ln.item_id,
                batch=dest_batch,
                kind=MovementKind.TRANSFER_IN,
                qty_delta=take,
                balance_after=take,
                ref_type="transfer",
                ref_id=str(transfer.id),
                actor=actor,
            )
            remaining_to_create -= take

    transfer.status = TransferStatus.DISCREPANCY if has_discrepancy else TransferStatus.RECEIVED
    if reason:
        transfer.diff_reason = reason
        if not transfer.reason:
            transfer.reason = reason
    transfer.received_by = actor
    transfer.received_at = datetime.now(tz=UTC)
    transfer.save(update_fields=["status", "reason", "diff_reason", "received_by", "received_at"])

    if has_discrepancy:
        notify_role(
            role=Role.MANAGER,
            kind="transfer_discrepancy",
            title_ar=f"فرق استلام في التحويل {transfer.number}",
            title_en=f"Discrepancy on transfer {transfer.number}",
            body_ar=reason,
            body_en=reason,
            ref_type="transfer",
            ref_id=transfer.id,
        )

    record_audit(
        actor=actor,
        action="transfer.receive",
        entity_type="transfer",
        entity_id=transfer.id,
        summary=f"Received transfer {transfer.number} ({transfer.status})",
        after={"status": transfer.status, "reason": reason},
    )
    from apps.catalog.features_services import trigger_back_in_stock_alerts

    trigger_back_in_stock_alerts([ln.item_id for ln, qty in line_qtys if qty > 0])
    return serialize_transfer(Transfer.objects.select_related("created_by", "received_by", "reviewed_by").prefetch_related("lines").get(id=transfer.id))


@transaction.atomic
def review_transfer(*, actor: User, transfer_id: int) -> dict[str, Any]:
    transfer = Transfer.objects.select_for_update().filter(id=transfer_id).first()
    if not transfer:
        raise ApiException(code=ErrorCode.NOT_FOUND, status_code=http_status.HTTP_404_NOT_FOUND)
    if transfer.status != TransferStatus.DISCREPANCY:
        raise ApiException(
            code=ErrorCode.BAD_STATE,
            message=("التحويل ليس في حالة فرق للمراجعة", "Transfer is not flagged for discrepancy review"),
        )
    transfer.status = TransferStatus.RECEIVED
    transfer.reviewed_by = actor
    transfer.reviewed_at = datetime.now(tz=UTC)
    transfer.save(update_fields=["status", "reviewed_by", "reviewed_at"])

    record_audit(
        actor=actor,
        action="transfer.review",
        entity_type="transfer",
        entity_id=transfer.id,
        summary=f"Manager reviewed discrepancy on {transfer.number}",
    )
    return serialize_transfer(Transfer.objects.select_related("created_by", "received_by", "reviewed_by").prefetch_related("lines").get(id=transfer.id))
