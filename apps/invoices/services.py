"""Invoice, credit note, batch allocation, and return management services."""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from django.apps import apps
from django.db import transaction
from django.db.models import Q
from rest_framework import status as http_status

from apps.accounts.business_rules import round_amount_by_rule
from apps.accounts.models import Role, SystemSetting, User
from apps.accounts.permissions import (
    get_active_customer_id,
    own_warehouse_id,
    sees_cost,
    sees_debt,
    sees_profit,
)
from apps.accounts.profiles import may_profile_action
from apps.accounts.rules_engine import run_automation_rules
from apps.audit.services import record_audit
from apps.catalog.models import Item
from apps.common.exceptions import ApiException, ErrorCode
from apps.common.utils import calc_percent_round
from apps.inventory.models import Batch, DamagedInventory, DamageSource, InventoryMovement, MovementKind
from apps.inventory.services import lock_and_deduct_fefo
from apps.invoices.models import (
    CreditNote,
    Invoice,
    InvoiceBatchAllocation,
    InvoiceLine,
    ReturnCondition,
    ReturnDoc,
    ReturnLine,
)
from apps.notifications.services import notify_customer, notify_role


def can_see_invoice(actor: User, inv: Invoice) -> bool:
    if actor.role in (Role.MANAGER, Role.ACCOUNTANT):
        return True
    own_wh = own_warehouse_id(actor)
    if own_wh is not None:
        return inv.warehouse_id == own_wh or inv.lines.filter(warehouse_id=own_wh).exists()
    if actor.role == Role.REP:
        return (
            inv.rep_id == actor.id
            or inv.delivery_rep_id == actor.id
            or (inv.customer is not None and inv.customer.rep_id == actor.id)
        )
    if actor.role == Role.SHOP:
        cid = get_active_customer_id(actor)
        return cid is not None and inv.customer_id == cid
    return False


def _fmt_num_dec(d: Decimal) -> int | float:
    return int(d) if d == int(d) else float(d)


def serialize_invoice(inv: Invoice, *, actor: User) -> dict[str, Any]:
    can_profit = sees_profit(actor)
    can_cost = sees_cost(actor)
    can_debt = sees_debt(actor)
    show_batches = actor.role not in (Role.SHOP, Role.REP)

    lines_out: list[dict[str, Any]] = []
    for ln in inv.lines.order_by("id"):
        line_dict: dict[str, Any] = {
            "id": ln.id,
            "item_id": ln.item_id,
            "warehouse_id": ln.warehouse_id,
            "unit": ln.unit,
            "qty": ln.qty,
            "pieces": ln.pieces,
            "returned_pieces": ln.returned_pieces,
            "unit_price": ln.unit_price,
            "total": ln.total,
        }
        if ln.offer_off:
            line_dict["offer_off"] = ln.offer_off
        if getattr(ln, "clearance", False):
            line_dict["clearance"] = True
        if show_batches:
            b_list = []
            for alloc in ln.batch_allocations.select_related("batch").order_by("id"):
                b_entry: dict[str, Any] = {
                    "batch_id": alloc.batch_id,
                    "expiry_date": alloc.batch.expiry_date.isoformat(),
                    "qty": alloc.qty_pieces,
                }
                if can_cost:
                    b_entry["purchase_price"] = alloc.purchase_price
                b_list.append(b_entry)
            line_dict["batches"] = b_list
        lines_out.append(line_dict)

    out: dict[str, Any] = {
        "id": inv.id,
        "number": inv.number,
        "date": inv.delivered_at.isoformat(),
        "order_id": inv.order_id,
        "customer_id": inv.customer_id,
        "rep_id": inv.rep_id or 0,
        "delivery_rep_id": inv.delivery_rep_id or 0,
        "warehouse_id": inv.warehouse_id,
        "subtotal": inv.subtotal,
        "discount_percent": _fmt_num_dec(inv.discount_percent),
        "discount": inv.discount,
        "rounding": inv.rounding,
        "total": inv.total,
        "usd_rate": _fmt_num_dec(inv.usd_rate),
        "returned": inv.returned,
        "credited": inv.credited,
        "status": inv.status_label,
        "lines": lines_out,
    }
    if can_debt:
        out["paid"] = inv.paid
        out["remaining"] = inv.remaining
    if can_profit:
        out["cost"] = inv.cost
        out["profit"] = inv.profit
    if inv.counter:
        out["counter"] = inv.counter
    if inv.created_by_id:
        out["created_by"] = {"id": inv.created_by_id, "name": inv.created_by.name}
    return out


def serialize_credit_note(cn: CreditNote) -> dict[str, Any]:
    actor_info = {
        "id": cn.created_by_id,
        "name": cn.created_by.name,
        "role": cn.created_by.role,
    }
    at_iso = cn.created_at.isoformat()
    return {
        "id": cn.id,
        "number": cn.number,
        "invoice_id": cn.invoice_id,
        "customer_id": cn.customer_id,
        "amount": cn.amount,
        "reason": cn.reason,
        "by": actor_info,
        "at": at_iso,
        "created_by": actor_info,
        "created_at": at_iso,
    }


def serialize_return(ret: ReturnDoc) -> dict[str, Any]:
    return {
        "id": ret.id,
        "number": ret.number,
        "client_ref": ret.client_ref or "",
        "invoice_id": ret.invoice_id,
        "customer_id": ret.customer_id,
        "rep_id": ret.rep_id or 0,
        "warehouse_id": ret.warehouse_id,
        "amount": ret.amount,
        "note": ret.note,
        "created_by": {
            "id": ret.created_by_id,
            "name": ret.created_by.name,
            "role": ret.created_by.role,
        },
        "created_at": ret.created_at.isoformat(),
        "lines": [
            {
                "id": rl.id,
                "item_id": rl.item_id,
                "batch_id": rl.batch_id,
                "condition": rl.condition,
                "unit": rl.unit,
                "qty": rl.qty,
                "pieces": rl.pieces,
                "unit_price": rl.unit_price,
                "amount": rl.amount,
            }
            for rl in ret.lines.order_by("id")
        ],
    }


def _invoice_base_qs():
    return Invoice.objects.select_related("customer", "order", "warehouse").prefetch_related(
        "lines__batch_allocations__batch"
    )


def list_invoices_for_actor(*, actor: User) -> list[dict[str, Any]]:
    qs = _invoice_base_qs().order_by("-delivered_at", "-id")
    own_wh = own_warehouse_id(actor)
    if own_wh is not None:
        qs = qs.filter(Q(warehouse_id=own_wh) | Q(lines__warehouse_id=own_wh)).distinct()
    elif actor.role == Role.REP:
        qs = qs.filter(Q(rep_id=actor.id) | Q(delivery_rep_id=actor.id) | Q(customer__rep_id=actor.id)).distinct()
    elif actor.role == Role.SHOP:
        cid = get_active_customer_id(actor)
        qs = qs.filter(customer_id=cid) if cid else qs.none()
    return [serialize_invoice(inv, actor=actor) for inv in qs]


def get_invoice_for_actor(*, actor: User, invoice_id: int) -> dict[str, Any]:
    inv = _invoice_base_qs().filter(id=invoice_id).first()
    if not inv:
        raise ApiException(code=ErrorCode.NOT_FOUND, status_code=http_status.HTTP_404_NOT_FOUND)
    if not can_see_invoice(actor, inv):
        raise ApiException(code=ErrorCode.FORBIDDEN, status_code=http_status.HTTP_403_FORBIDDEN)
    return serialize_invoice(inv, actor=actor)


def create_invoice_for_order(
    *,
    order,
    delivered_line_specs: list[dict[str, Any]],
    delivery_rep: User | None,
    actor: User,
    delivered_at: datetime | None = None,
) -> Invoice:
    """
    Create an immutable Invoice from delivered order lines, consuming FEFO batches atomically.
    Caller must already hold an @transaction.atomic block.
    """
    dlv_time = delivered_at or datetime.now(tz=UTC)
    usd_rate = SystemSetting.get_solo().usd_rate

    subtotal = sum(spec["total"] for spec in delivered_line_specs)
    discount_pct = order.discount_percent or Decimal("0")
    discount = calc_percent_round(subtotal, discount_pct)
    after = max(0, subtotal - discount)
    total = round_amount_by_rule(after)
    rounding = total - after

    inv = Invoice.objects.create(
        number=f"INV-TMP-{order.id}",
        order=order,
        customer_id=order.customer_id,
        rep_id=order.rep_id,
        delivery_rep=delivery_rep,
        warehouse_id=order.warehouse_id,
        subtotal=subtotal,
        discount_percent=discount_pct,
        discount=discount,
        rounding=rounding,
        total=total,
        cost=0,
        usd_rate=usd_rate,
        created_by=delivery_rep or order.created_by,
        delivered_at=dlv_time,
    )
    inv.number = f"INV-{inv.id:05d}"

    total_cost = 0
    for spec in delivered_line_specs:
        wh_id = spec["warehouse_id"]
        item_id = spec["item_id"]
        pieces = spec["pieces"]

        allocs = lock_and_deduct_fefo(
            warehouse_id=wh_id,
            item_id=item_id,
            qty_pieces=pieces,
            actor=actor,
            kind=MovementKind.SALE,
            ref_type="invoice",
            ref_id=inv.id,
        )
        line_cost = sum(take * b.purchase_price for b, take in allocs)
        total_cost += line_cost

        inv_line = InvoiceLine.objects.create(
            invoice=inv,
            warehouse_id=wh_id,
            item_id=item_id,
            unit=spec["unit"],
            qty=spec["qty"],
            pieces=pieces,
            returned_pieces=0,
            unit_price=spec["unit_price"],
            total=spec["total"],
            cost=line_cost,
        )
        for batch, take in allocs:
            InvoiceBatchAllocation.objects.create(
                invoice=inv,
                invoice_line=inv_line,
                batch=batch,
                customer_id=order.customer_id,
                qty_pieces=take,
                returned_pieces=0,
                purchase_price=batch.purchase_price,
            )

    inv.cost = total_cost
    inv.save(update_fields=["number", "cost"])

    from apps.payments.services import apply_available_credit_to_open_invoices

    apply_available_credit_to_open_invoices(customer_id=order.customer_id)
    inv.refresh_from_db(fields=["paid"])
    return inv


@transaction.atomic
def create_credit_note(*, actor: User, invoice_id: int, data: dict[str, Any]) -> dict[str, Any]:
    if actor.role not in (Role.MANAGER, Role.ACCOUNTANT):
        raise ApiException(code=ErrorCode.FORBIDDEN, status_code=http_status.HTTP_403_FORBIDDEN)

    inv = Invoice.objects.select_for_update().filter(id=invoice_id).first()
    if not inv:
        raise ApiException(code=ErrorCode.NOT_FOUND, status_code=http_status.HTTP_404_NOT_FOUND)

    try:
        amount = int(data.get("amount"))
    except (TypeError, ValueError) as exc:
        msg = ("أدخل مبلغاً صحيحاً", "Enter a valid amount")
        raise ApiException(code=ErrorCode.VALIDATION, message=msg, errors={"amount": msg}) from exc

    if amount <= 0:
        msg = ("مبلغ التعويض يجب أن يكون أكبر من صفر", "Credit amount must be positive")
        raise ApiException(code=ErrorCode.VALIDATION, message=msg, errors={"amount": msg})

    rem = max(0, inv.remaining)
    if amount > rem:
        msg = ("المبلغ يتجاوز المتبقي على الفاتورة", "Amount exceeds invoice remaining balance")
        raise ApiException(code=ErrorCode.VALIDATION, message=msg, errors={"amount": msg})

    reason = (str(data.get("reason") or "")).strip()
    if not reason:
        msg = ("سبب التعويض مطلوب", "Credit note reason is required")
        raise ApiException(code=ErrorCode.VALIDATION, message=msg, errors={"reason": msg})

    cn = CreditNote.objects.create(
        number="CN-TMP",
        invoice=inv,
        customer_id=inv.customer_id,
        amount=amount,
        reason=reason,
        created_by=actor,
    )
    cn.number = f"CN-{cn.id:05d}"
    cn.save(update_fields=["number"])

    inv.credited += amount
    inv.save(update_fields=["credited"])

    notify_customer(
        customer_id=inv.customer_id,
        kind="credit_note",
        title_ar=f"إشعار دائن على الفاتورة {inv.number}",
        title_en=f"Credit note on invoice {inv.number}",
        body_ar=f"تم خصم {amount:,} د.ع: {reason}",
        body_en=f"{amount:,} IQD credited: {reason}",
        ref_type="invoice",
        ref_id=inv.id,
    )
    record_audit(
        actor=actor,
        action="create",
        entity_type="invoice",
        entity_id=inv.id,
        summary=f"Created credit note {cn.number} ({amount:,} IQD) on invoice {inv.number}",
        summary_ar=f"إنشاء إشعار دائن {cn.number} بمبلغ {amount:,} د.ع على الفاتورة {inv.number}",
        after={"credit_note_id": cn.id, "amount": amount, "reason": reason},
    )
    return serialize_credit_note(cn)


def list_returns_for_actor(*, actor: User) -> list[dict[str, Any]]:
    if actor.role not in (Role.MANAGER, Role.ACCOUNTANT, Role.WAREHOUSE, Role.REP, Role.SHOP):
        raise ApiException(code=ErrorCode.FORBIDDEN, status_code=http_status.HTTP_403_FORBIDDEN)
    qs = ReturnDoc.objects.select_related("created_by", "invoice", "customer").prefetch_related("lines").order_by("-id")
    own_wh = own_warehouse_id(actor)
    if own_wh is not None:
        qs = qs.filter(Q(warehouse_id=own_wh) | Q(invoice__lines__warehouse_id=own_wh)).distinct()
    elif actor.role == Role.REP:
        qs = qs.filter(Q(rep_id=actor.id) | Q(customer__rep_id=actor.id) | Q(invoice__delivery_rep_id=actor.id)).distinct()
    elif actor.role == Role.SHOP:
        cid = get_active_customer_id(actor)
        qs = qs.filter(customer_id=cid) if cid else qs.none()
    return [serialize_return(r) for r in qs]


def get_return_for_actor(*, actor: User, return_id: int) -> dict[str, Any]:
    ret = (
        ReturnDoc.objects.select_related("created_by", "invoice", "customer")
        .prefetch_related("lines")
        .filter(id=return_id)
        .first()
    )
    if not ret:
        raise ApiException(code=ErrorCode.NOT_FOUND, status_code=http_status.HTTP_404_NOT_FOUND)
    if not can_see_invoice(actor, ret.invoice):
        raise ApiException(code=ErrorCode.FORBIDDEN, status_code=http_status.HTTP_403_FORBIDDEN)
    return serialize_return(ret)


@transaction.atomic
def create_return(*, actor: User, data: dict[str, Any]) -> dict[str, Any]:
    if actor.role not in (Role.MANAGER, Role.ACCOUNTANT, Role.WAREHOUSE, Role.REP):
        raise ApiException(code=ErrorCode.FORBIDDEN, status_code=http_status.HTTP_403_FORBIDDEN)
    may_profile_action(actor, "returns")

    client_ref = (str(data.get("client_ref") or "")).strip() or None
    if client_ref:
        existing = (
            ReturnDoc.objects.select_related("created_by", "invoice", "customer")
            .prefetch_related("lines")
            .filter(client_ref=client_ref)
            .first()
        )
        if existing:
            return serialize_return(existing)

    inv_id = data.get("invoice_id")
    inv = Invoice.objects.select_for_update().select_related("customer").filter(id=inv_id).first()
    if not inv:
        raise ApiException(code=ErrorCode.NOT_FOUND, message=("الفاتورة غير موجودة", "Invoice not found"), status_code=http_status.HTTP_404_NOT_FOUND)

    own_wh = own_warehouse_id(actor)
    if own_wh is not None and inv.warehouse_id != own_wh and not inv.lines.filter(warehouse_id=own_wh).exists():
        raise ApiException(code=ErrorCode.FORBIDDEN, status_code=http_status.HTTP_403_FORBIDDEN)
    if actor.role == Role.REP and not can_see_invoice(actor, inv):
        raise ApiException(code=ErrorCode.FORBIDDEN, status_code=http_status.HTTP_403_FORBIDDEN)

    raw_lines = data.get("lines")
    if not isinstance(raw_lines, list) or not raw_lines:
        raise ApiException(code=ErrorCode.VALIDATION, message=("أضف مادة واحدة على الأقل", "At least one return line is required"))

    note = (str(data.get("note") or "")).strip()
    inv_lines = list(InvoiceLine.objects.select_for_update().filter(invoice=inv).order_by("id"))

    # Aggregate returnable pieces per item on this invoice
    returnable_by_item: dict[int, int] = {}
    for il in inv_lines:
        returnable_by_item[il.item_id] = returnable_by_item.get(il.item_id, 0) + (il.pieces - il.returned_pieces)

    item_map = {it.id: it for it in Item.objects.filter(id__in=[ln.get("item_id") for ln in raw_lines])}
    parsed_lines: list[dict[str, Any]] = []
    requested_by_item: dict[int, int] = {}

    for idx, raw in enumerate(raw_lines):
        item_id = raw.get("item_id")
        item = item_map.get(item_id)
        if not item:
            raise ApiException(code=ErrorCode.VALIDATION, message=("المادة غير موجودة", "Item not found"), errors={f"lines.{idx}.item_id": ("المادة غير موجودة", "Item not found")})
        unit = str(raw.get("unit") or "piece").strip().lower()
        if unit not in ("carton", "piece"):
            raise ApiException(code=ErrorCode.VALIDATION, message=("وحدة غير صالحة", "Invalid unit"))
        try:
            qty = int(raw.get("qty"))
        except (TypeError, ValueError) as exc:
            raise ApiException(code=ErrorCode.VALIDATION, message=("كمية غير صالحة", "Invalid quantity")) from exc
        if qty <= 0:
            continue
        cond = str(raw.get("condition") or ReturnCondition.GOOD).strip().lower()
        if cond not in (ReturnCondition.GOOD, ReturnCondition.DAMAGED):
            raise ApiException(code=ErrorCode.VALIDATION, message=("حالة المادة غير صالحة", "Invalid item condition"))

        pieces = qty * item.pieces_per_carton if unit == "carton" else qty
        requested_by_item[item.id] = requested_by_item.get(item.id, 0) + pieces
        if requested_by_item[item.id] > returnable_by_item.get(item.id, 0):
            raise ApiException(
                code=ErrorCode.RETURN_TOO_MUCH,
                message=("الكمية المرتجعة تتجاوز المتاح في الفاتورة", "Return quantity exceeds remaining delivered quantity"),
            )
        parsed_lines.append({"item": item, "unit": unit, "qty": qty, "pieces": pieces, "condition": cond})

    if not parsed_lines:
        raise ApiException(code=ErrorCode.VALIDATION, message=("اختر كمية مرتجعة", "Select at least one returned quantity"))

    # Effective invoice discount/rounding factor
    net_factor = Decimal(inv.total) / Decimal(inv.subtotal) if inv.subtotal > 0 else Decimal("1")

    ret_doc = ReturnDoc.objects.create(
        number="RET-TMP",
        invoice=inv,
        customer_id=inv.customer_id,
        rep_id=inv.rep_id,
        warehouse_id=inv.warehouse_id,
        amount=0,
        cost_recovered=0,
        commission_base=0,
        note=note,
        client_ref=client_ref,
        created_by=actor,
    )
    ret_doc.number = f"RET-{ret_doc.id:05d}"

    prev_returned = inv.returned
    total_ret_amount = 0
    total_good_cost = 0

    for spec in parsed_lines:
        item = spec["item"]
        rem_pieces = spec["pieces"]
        cond = spec["condition"]

        # Match against invoice lines for this item
        matching_lines = [il for il in inv_lines if il.item_id == item.id and il.pieces > il.returned_pieces]
        line_amount = 0
        first_il = matching_lines[0] if matching_lines else None
        for il in matching_lines:
            if rem_pieces <= 0:
                break
            can_take = il.pieces - il.returned_pieces
            take = min(can_take, rem_pieces)
            il.returned_pieces += take
            il.save(update_fields=["returned_pieces"])
            rem_pieces -= take

            piece_val = (Decimal(il.total) / Decimal(il.pieces)) * net_factor
            part_amt = int((piece_val * Decimal(take)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
            line_amount += part_amt

        # Restore / record batches from InvoiceBatchAllocation
        alloc_rem = spec["pieces"]
        allocs = list(
            InvoiceBatchAllocation.objects.select_for_update()
            .select_related("batch")
            .filter(invoice=inv, invoice_line__item_id=item.id, qty_pieces__gt=models_F_returned())
            .order_by("id")
        )
        # Filter in python safely for returned_pieces < qty_pieces
        open_allocs = [a for a in allocs if a.qty_pieces > a.returned_pieces]
        first_batch_id = open_allocs[0].batch_id if open_allocs else None
        line_cost = 0

        for alloc in open_allocs:
            if alloc_rem <= 0:
                break
            can_alloc = alloc.qty_pieces - alloc.returned_pieces
            take_a = min(can_alloc, alloc_rem)
            alloc.returned_pieces += take_a
            alloc.save(update_fields=["returned_pieces"])
            alloc_rem -= take_a

            if cond == ReturnCondition.GOOD:
                batch = Batch.objects.select_for_update().get(id=alloc.batch_id)
                batch.qty_left += take_a
                batch.save(update_fields=["qty_left"])
                InventoryMovement.objects.create(
                    warehouse_id=batch.warehouse_id,
                    item=item,
                    batch=batch,
                    kind=MovementKind.RETURN_GOOD,
                    qty_delta=take_a,
                    balance_after=batch.qty_left,
                    ref_type="return",
                    ref_id=str(ret_doc.id),
                    actor=actor,
                )
                c_part = take_a * alloc.purchase_price
                line_cost += c_part
                total_good_cost += c_part
            else:
                DamagedInventory.objects.create(
                    warehouse_id=alloc.batch.warehouse_id,
                    item=item,
                    batch=alloc.batch,
                    pieces=take_a,
                    source=DamageSource.RETURN,
                    ref=ret_doc.number,
                    reason=note,
                    recorded_by=actor,
                )

        unit_price_eff = int((Decimal(line_amount) / Decimal(spec["qty"])).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        ReturnLine.objects.create(
            return_doc=ret_doc,
            invoice_line=first_il,
            item=item,
            batch_id=first_batch_id,
            condition=cond,
            unit=spec["unit"],
            qty=spec["qty"],
            pieces=spec["pieces"],
            unit_price=unit_price_eff,
            amount=line_amount,
            cost=line_cost,
        )
        total_ret_amount += line_amount

    # Prevent tiny rounding drift on full-invoice return
    if all(il.returned_pieces == il.pieces for il in inv_lines) and inv.credited == 0:
        expected_total_ret = inv.total
        total_ret_amount = max(0, expected_total_ret - prev_returned)

    inv.returned = prev_returned + total_ret_amount
    inv.cost = max(0, inv.cost - total_good_cost)
    inv.save(update_fields=["returned", "cost"])

    # Commission clawback proportion against already collected cash on this invoice
    before_cap = max(0, min(inv.paid, inv.total - prev_returned - inv.credited))
    after_cap = max(0, min(inv.paid, inv.total - inv.returned - inv.credited))
    comm_clawback = max(0, before_cap - after_cap)

    ret_doc.amount = total_ret_amount
    ret_doc.cost_recovered = total_good_cost
    ret_doc.commission_base = comm_clawback
    ret_doc.save(update_fields=["number", "amount", "cost_recovered", "commission_base"])

    notify_role(
        role=Role.MANAGER,
        kind="return",
        title_ar=f"مرتجع {ret_doc.number} على الفاتورة {inv.number}",
        title_en=f"Return {ret_doc.number} on invoice {inv.number}",
        body_ar=f"المبلغ: {total_ret_amount:,} د.ع",
        body_en=f"Amount: {total_ret_amount:,} IQD",
        ref_type="return",
        ref_id=ret_doc.id,
    )
    record_audit(
        actor=actor,
        action="create",
        entity_type="return",
        entity_id=ret_doc.id,
        summary=f"Created return {ret_doc.number} ({total_ret_amount:,} IQD) on {inv.number}",
        summary_ar=f"تسجيل مرتجع {ret_doc.number} بمبلغ {total_ret_amount:,} د.ع على الفاتورة {inv.number}",
        after={"invoice_id": inv.id, "amount": total_ret_amount, "commission_base": comm_clawback},
    )
    run_automation_rules(
        "return_created",
        {"amount": total_ret_amount},
        number=ret_doc.number,
        link=f"/invoices/{inv.id}",
        by=actor.name,
        customer=inv.customer.name,
    )
    return serialize_return(ReturnDoc.objects.select_related("created_by").prefetch_related("lines").get(id=ret_doc.id))


def models_F_returned():
    from django.db.models import F

    return F("returned_pieces")


@transaction.atomic
def record_counter_sale(*, actor: User, data: dict[str, Any]) -> dict[str, Any]:
    """Execute counter sale: FEFO deduction, paid invoice generation, and cash/transfer payment."""
    import re

    from django.db.models import Max
    from django.utils import timezone

    if actor.role != Role.WAREHOUSE:
        raise ApiException(
            code=ErrorCode.FORBIDDEN,
            status_code=http_status.HTTP_403_FORBIDDEN,
            message=("أمين المخزن فقط", "Warehouse keeper only"),
        )
    client_ref = str(data.get("client_ref") or "").strip()
    if not client_ref:
        raise ApiException(
            code=ErrorCode.VALIDATION,
            message=("الرقم المرجعي مطلوب", "client_ref is required"),
            errors={"client_ref": ("الرقم المرجعي مطلوب", "client_ref is required")},
        )

    from apps.orders.models import Order

    existing_order = Order.objects.filter(client_ref=client_ref).first()
    if existing_order:
        inv = getattr(existing_order, "invoice", None)
        if inv:
            return serialize_invoice(inv, actor=actor)
        raise ApiException(
            code=ErrorCode.BAD_STATE,
            status_code=http_status.HTTP_409_CONFLICT,
            message=("هذه العملية مسجّلة من قبل", "This was already recorded"),
        )

    try:
        warehouse_id = int(data.get("warehouse_id") or 0)
    except (TypeError, ValueError):
        warehouse_id = 0

    from apps.warehouses.models import Warehouse
    from apps.warehouses.services import works_in_warehouse

    wh = Warehouse.objects.filter(id=warehouse_id).first()
    if not wh or not works_in_warehouse(actor, warehouse_id):
        raise ApiException(
            code=ErrorCode.FORBIDDEN,
            status_code=http_status.HTTP_403_FORBIDDEN,
            message=("تبيع من مخزنك فقط", "You sell only from your warehouse"),
        )

    from apps.accounts.models import PriceTier

    try:
        tier_id = int(data.get("tier_id") or 0)
    except (TypeError, ValueError):
        tier_id = 0
    tier = PriceTier.objects.filter(id=tier_id).first()
    if not tier:
        raise ApiException(
            code=ErrorCode.VALIDATION,
            message=("اختر مستوى السعر", "Choose the price level"),
            errors={"tier_id": ("اختر مستوى السعر", "Choose the price level")},
        )

    method = str(data.get("method") or "").strip().lower()
    if method not in ("cash", "transfer"):
        raise ApiException(
            code=ErrorCode.VALIDATION,
            message=("اختر طريقة الدفع", "Choose how he pays"),
            errors={"method": ("اختر طريقة الدفع", "Choose how he pays")},
        )

    buyer_name = str(data.get("buyer_name") or "").strip()[:80]
    buyer_phone = str(data.get("buyer_phone") or "").replace(" ", "").replace("-", "").strip()
    if buyer_phone and not re.match(r"^07\d{9}$", buyer_phone):
        raise ApiException(
            code=ErrorCode.VALIDATION,
            message=("رقم عراقي من 11 رقماً يبدأ بـ 07", "An 11-digit Iraqi number starting 07"),
            errors={"buyer_phone": ("رقم عراقي من 11 رقماً يبدأ بـ 07", "An 11-digit Iraqi number starting 07")},
        )

    raw_lines = data.get("lines")
    if not isinstance(raw_lines, list) or not raw_lines:
        raise ApiException(code=ErrorCode.VALIDATION, message=("قائمة المواد مطلوبة", "Lines are required"))

    from apps.customers.models import Customer

    walk_in_cust = Customer.objects.filter(walk_in=True).first()
    if not walk_in_cust:
        default_tier = PriceTier.objects.order_by("id").last() or tier
        walk_in_cust = Customer.objects.create(
            name="زبون مباشر",
            owner_name="بيع مباشر من المخزن",
            phone="",
            city="",
            area="",
            street="",
            landmark="",
            address_text="",
            debt_limit=0,
            tier=default_tier,
            rep=None,
            active=True,
            walk_in=True,
        )

    from apps.catalog.models import Item

    item_ids = [int(line.get("item_id", 0)) for line in raw_lines if isinstance(line, dict)]
    items_by_id = {it.id: it for it in Item.objects.prefetch_related("prices").filter(id__in=item_ids)}

    now = timezone.now()
    today_str = now.strftime("%Y-%m-%d")

    parsed_lines = []
    subtotal = 0
    total_cost = 0

    for idx, line in enumerate(raw_lines):
        item_id = int(line.get("item_id", 0))
        unit = str(line.get("unit") or "").lower()
        qty = int(line.get("qty", 0))
        it = items_by_id.get(item_id)
        if not it or it.deleted or not it.active:
            raise ApiException(
                code=ErrorCode.VALIDATION,
                message=(f"المادة {item_id} غير متاحة", f"Item {item_id} unavailable"),
            )
        if unit not in ("carton", "piece"):
            raise ApiException(code=ErrorCode.VALIDATION, message=("الوحدة غير صحيحة", "Invalid unit"))
        if it.sells == "pack" and unit == "piece":
            raise ApiException(
                code=ErrorCode.VALIDATION,
                message=("المادة تباع بالكارتون فقط", "Item sold in carton only"),
                errors={f"lines.{idx}.unit": ("تباع بالكارتون فقط", "Carton only")},
            )
        if it.sells == "piece" and unit == "carton":
            raise ApiException(
                code=ErrorCode.VALIDATION,
                message=("المادة تباع بالقطعة فقط", "Item sold in piece only"),
                errors={f"lines.{idx}.unit": ("تباع بالقطعة فقط", "Piece only")},
            )
        if qty <= 0:
            raise ApiException(code=ErrorCode.VALIDATION, message=("الكمية يجب أن تكون موجبة", "Quantity must be > 0"))

        pieces = qty * it.pieces_per_carton if unit == "carton" else qty

        tp = next((p for p in it.prices.all() if p.tier_id == tier.id), None)
        base_unit_price = (tp.carton_price if unit == "carton" else tp.piece_price) if tp else 0
        if base_unit_price <= 0:
            t1 = next((p for p in it.prices.all() if p.tier_id == 1), None)
            base_unit_price = (t1.carton_price if unit == "carton" else t1.piece_price) if t1 else 0

        offer_off = 0
        if it.offer and isinstance(it.offer, dict):
            o_unit = it.offer.get("unit")
            o_min = it.offer.get("min", 2)
            o_off = it.offer.get("off", 0)
            o_until = it.offer.get("until")
            if o_unit == unit and qty >= o_min and (not o_until or str(o_until) >= today_str):
                offer_off = int(o_off)

        final_unit_price = max(0, base_unit_price - offer_off)
        line_total = final_unit_price * qty
        subtotal += line_total

        parsed_lines.append(
            {
                "item": it,
                "unit": unit,
                "qty": qty,
                "pieces": pieces,
                "unit_price": final_unit_price,
                "offer_off": offer_off,
                "total": line_total,
            }
        )

    total = round_amount_by_rule(subtotal)
    rounding = total - subtotal
    received = None
    if method == "cash":
        rec_val = data.get("received")
        if rec_val is not None:
            try:
                received = int(rec_val)
                if received < total:
                    raise ApiException(
                        code=ErrorCode.VALIDATION,
                        message=("المبلغ المستلم أقل من المجموع", "The cash handed over is less than the total"),
                        errors={"received": (f"المجموع {total:,} د.ع", f"The total is {total:,} IQD")},
                    )
            except (TypeError, ValueError) as exc:
                if isinstance(exc, ApiException):
                    raise
                raise ApiException(code=ErrorCode.VALIDATION, message=("المبلغ المستلم غير صالح", "Invalid received amount")) from exc

    counter_info: dict[str, Any] = {
        "tier_id": tier.id,
        "method": method,
    }
    if buyer_name:
        counter_info["buyer_name"] = buyer_name
    if buyer_phone:
        counter_info["buyer_phone"] = buyer_phone
    if received is not None:
        counter_info["received"] = received

    fefo_deductions = []
    for pl in parsed_lines:
        it = pl["item"]
        pieces = pl["pieces"]
        allocs = lock_and_deduct_fefo(
            warehouse_id=wh.id,
            item_id=it.id,
            qty_pieces=pieces,
            actor=actor,
            kind=MovementKind.SALE,
            ref_type="order",
            ref_id=0,
        )
        line_cost = sum(take * b.purchase_price for b, take in allocs)
        total_cost += line_cost
        fefo_deductions.append((pl, allocs, line_cost))

    order_id = (Order.objects.aggregate(m=Max("id"))["m"] or 0) + 1
    order_num = f"ORD-{order_id:06d}"

    order = Order.objects.create(
        id=order_id,
        number=order_num,
        customer=walk_in_cust,
        warehouse=wh,
        rep=None,
        created_by=actor,
        source="counter",
        status="confirmed",
        subtotal=subtotal,
        rounding=rounding,
        total=total,
        client_ref=client_ref,
        counter=counter_info,
        accepted_by=actor,
        accepted_at=now,
        approved_by=actor,
        approved_at=now,
        notes=str(data.get("note") or "").strip(),
    )

    from apps.orders.models import OrderLine

    for pl in parsed_lines:
        OrderLine.objects.create(
            order=order,
            item=pl["item"],
            warehouse=wh,
            unit=pl["unit"],
            qty=pl["qty"],
            pieces=pl["pieces"],
            unit_price=pl["unit_price"],
            offer_off=pl["offer_off"],
            total=pl["total"],
        )

    inv_id = (Invoice.objects.aggregate(m=Max("id"))["m"] or 0) + 1
    inv_num = f"INV-{inv_id:06d}"

    usd_rate_val = Decimal("1500.00")
    if apps.is_installed("apps.exchange_rates"):
        from apps.exchange_rates.models import UsdRate

        latest_usd = UsdRate.objects.order_by("-date").first()
        if latest_usd:
            usd_rate_val = latest_usd.rate

    inv = Invoice.objects.create(
        id=inv_id,
        number=inv_num,
        order=order,
        customer=walk_in_cust,
        rep=None,
        delivery_rep=None,
        warehouse=wh,
        subtotal=subtotal,
        discount_percent=Decimal("0.00"),
        discount=0,
        rounding=rounding,
        total=total,
        cost=total_cost,
        paid=total,
        returned=0,
        credited=0,
        usd_rate=usd_rate_val,
        created_by=actor,
        delivered_at=now,
        counter=counter_info,
    )

    for pl, deductions, line_cost in fefo_deductions:
        inv_line = InvoiceLine.objects.create(
            invoice=inv,
            warehouse=wh,
            item=pl["item"],
            unit=pl["unit"],
            qty=pl["qty"],
            pieces=pl["pieces"],
            returned_pieces=0,
            unit_price=pl["unit_price"],
            offer_off=pl["offer_off"],
            total=pl["total"],
            cost=line_cost,
        )
        for batch, used_pieces in deductions:
            InvoiceBatchAllocation.objects.create(
                invoice=inv,
                invoice_line=inv_line,
                batch=batch,
                customer=walk_in_cust,
                qty_pieces=used_pieces,
                purchase_price=batch.purchase_price,
            )

    from apps.payments.models import Payment, PaymentAllocation, PaymentMethod

    pay_id = (Payment.objects.aggregate(m=Max("id"))["m"] or 0) + 1
    pay_num = f"PAY-{pay_id:06d}"
    pay_method = PaymentMethod.CASH if method == "cash" else PaymentMethod.TRANSFER

    payment = Payment.objects.create(
        id=pay_id,
        number=pay_num,
        client_ref=f"{client_ref}-pay",
        customer=walk_in_cust,
        received_by=actor,
        received_by_name=actor.name,
        received_by_role=actor.role,
        method=pay_method,
        amount=total,
        unallocated_amount=0,
        note=f"بيع مباشر: {buyer_name}" if buyer_name else "بيع مباشر",
    )
    PaymentAllocation.objects.create(
        payment=payment,
        invoice=inv,
        amount=total,
    )

    record_audit(
        actor=actor,
        action="counter_sale",
        entity_type="invoice",
        entity_id=inv.id,
        summary=f"Counter sale {inv.number}: {total:,} IQD ({method})",
    )

    return serialize_invoice(inv, actor=actor)

