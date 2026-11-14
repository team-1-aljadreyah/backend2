"""Delivery QR dispatch, persistent 5-attempt PIN lock, proof photo, partial split, and problem recovery."""
from __future__ import annotations

import hmac
import math
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

from django.conf import settings
from django.contrib.auth.hashers import check_password
from django.db import transaction
from django.db.models import Q
from rest_framework import status as http_status

from apps.accounts.business_rules import DOOR_METHODS_ALL, round_amount_by_rule, rules_of
from apps.accounts.models import Role, SystemSetting, User
from apps.accounts.permissions import (
    get_active_customer_id,
    own_warehouse_id,
    rep_may_deliver,
    works_in_warehouse,
)
from apps.accounts.serializers import serialize_vehicle
from apps.audit.services import record_audit
from apps.catalog.services import item_piece_weight_kg
from apps.common.exceptions import ApiException, ErrorCode
from apps.common.utils import calc_percent_round, decode_image_data_url, decrypt_secret
from apps.customers.services import customer_balance_map
from apps.deliveries.models import Delivery, DeliveryStatus, RepLocation, Vehicle
from apps.invoices.services import create_invoice_for_order
from apps.notifications.services import notify_customer, notify_role, notify_user
from apps.orders.models import Order, OrderLine, OrderStatus
from apps.orders.services import can_see_order, get_order_for_actor
from apps.payments.custody_services import check_rep_custody_cap
from apps.payments.models import Payment, PaymentMethod
from apps.payments.services import _allocate_payment_fifo
from apps.realtime.broadcaster import publish_order_changed, publish_rep_location
from integrations.storage import get_storage

MAX_PIN_ATTEMPTS = 5
VALID_PROBLEM_REASONS = frozenset({"accident", "vehicle", "shop_closed", "no_answer", "refused", "other"})


def _normalize_delivery_code(raw: str | None) -> str:
    s = (raw or "").strip()
    if s.upper().startswith("NAHRAIN:DLV:"):
        s = s[12:]
    return re.sub(r"[^A-Z0-9]", "", s.upper())


@transaction.atomic
def dispatch_delivery(*, actor: User, code: str) -> dict[str, Any]:
    if not (actor.role == Role.MANAGER or (actor.role == Role.REP and rep_may_deliver(actor))):
        raise ApiException(code=ErrorCode.FORBIDDEN, status_code=http_status.HTTP_403_FORBIDDEN)

    norm = _normalize_delivery_code(code)
    if not norm:
        raise ApiException(code=ErrorCode.VALIDATION, message=("رمز الشحنة مطلوب", "Shipment code is required"), errors={"code": ("رمز الشحنة مطلوب", "Shipment code is required")})

    dlv = Delivery.objects.select_for_update().select_related("order", "order__customer").filter(code=norm).first()
    if not dlv:
        raise ApiException(
            code=ErrorCode.NOT_FOUND,
            message=("رمز الشحنة غير موجود", "Shipment code not found"),
            status_code=http_status.HTTP_404_NOT_FOUND,
        )

    if dlv.status == DeliveryStatus.DELIVERED:
        raise ApiException(code=ErrorCode.BAD_STATE, message=("تم تسليم هذه الشحنة مسبقاً", "Shipment already delivered"))
    if dlv.order.status in (OrderStatus.CANCELLED, OrderStatus.EXPIRED):
        raise ApiException(code=ErrorCode.BAD_STATE, message=("الطلب ملغي أو منتهي", "Order is cancelled or expired"))

    if dlv.rep_id is not None and dlv.rep_id != actor.id and actor.role != Role.MANAGER:
        raise ApiException(
            code=ErrorCode.FORBIDDEN,
            message=("هذه الشحنة مسندة لمندوب آخر", "Shipment is assigned to another delivery rep"),
            status_code=http_status.HTTP_403_FORBIDDEN,
        )

    # Idempotent if same rep scans already-dispatched shipment
    if dlv.status == DeliveryStatus.OUT and dlv.rep_id == actor.id and not dlv.problem_reason:
        return get_order_for_actor(actor=actor, order_id=dlv.order_id)

    now = datetime.now(tz=UTC)
    dlv.rep = actor if actor.role == Role.REP else dlv.rep
    dlv.dispatched_by = actor
    dlv.out_by = actor
    dlv.out_at = now
    dlv.out_phone = actor.phone or ""
    dlv.status = DeliveryStatus.OUT
    dlv.dispatched_at = now
    dlv.problem_reason = ""
    dlv.problem_note = ""
    dlv.problem_by = None
    dlv.problem_at = None
    dlv.save(
        update_fields=[
            "rep",
            "dispatched_by",
            "out_by",
            "out_at",
            "out_phone",
            "status",
            "dispatched_at",
            "problem_reason",
            "problem_note",
            "problem_by",
            "problem_at",
        ]
    )

    pin = ""
    try:
        pin = decrypt_secret(dlv.pin_encrypted)
    except Exception:
        pin = ""

    notify_customer(
        customer_id=dlv.order.customer_id,
        kind="out_for_delivery",
        title_ar=f"شحنتك {dlv.order.number} في الطريق إليك",
        title_en=f"Your order {dlv.order.number} is out for delivery",
        body_ar=f"رمز التسليم الخاص بك: {pin}" if pin else "",
        body_en=f"Your delivery PIN: {pin}" if pin else "",
        ref_type="order",
        ref_id=dlv.order_id,
    )
    record_audit(
        actor=actor,
        action="update",
        entity_type="order",
        entity_id=dlv.order_id,
        summary=f"Dispatched order {dlv.order.number} ({dlv.code})",
        summary_ar=f"إخراج الشحنة {dlv.order.number} للتوصيل ({dlv.code})",
    )
    publish_order_changed(dlv.order_id)
    return get_order_for_actor(actor=actor, order_id=dlv.order_id)


def _check_delivery_pin_step(*, actor: User, order_id: int, raw_pin: str) -> dict[str, Any]:
    """
    Isolated atomic step verifying driver authorization & PIN.
    CRITICAL: Commits incremented pin_attempts / 5-try lock even when returning { ok: false }.
    """
    with transaction.atomic():
        dlv = Delivery.objects.select_for_update().select_related("order").filter(order_id=order_id).first()
        if not dlv:
            raise ApiException(code=ErrorCode.NOT_FOUND, status_code=http_status.HTTP_404_NOT_FOUND)

        if actor.role != Role.MANAGER:
            if actor.role != Role.REP or not rep_may_deliver(actor) or dlv.rep_id != actor.id:
                raise ApiException(code=ErrorCode.FORBIDDEN, status_code=http_status.HTTP_403_FORBIDDEN)

        if dlv.status != DeliveryStatus.OUT or dlv.order.status in (OrderStatus.CONFIRMED, OrderStatus.CANCELLED, OrderStatus.EXPIRED):
            raise ApiException(
                code=ErrorCode.BAD_STATE,
                message=("الشحنة ليست قيد التوصيل", "Shipment is not out for delivery"),
            )

        if dlv.locked:
            return {"ok": False, "left": 0, "locked": True}

        submitted = (raw_pin or "").strip()
        if dlv.pin_hash:
            matched = bool(submitted) and check_password(submitted, dlv.pin_hash)
        else:
            expected_pin = decrypt_secret(dlv.pin_encrypted)
            matched = bool(submitted) and hmac.compare_digest(submitted, expected_pin)

        if not matched:
            pin_tries = max(1, int(rules_of().get("pin_tries") or MAX_PIN_ATTEMPTS))
            dlv.pin_attempts += 1
            left = max(0, pin_tries - dlv.pin_attempts)
            if dlv.pin_attempts >= pin_tries:
                dlv.locked = True
                notify_role(
                    role=Role.MANAGER,
                    kind="delivery_locked",
                    title_ar=f"قفل تسليم الشحنة {dlv.order.number}",
                    title_en=f"Delivery locked for {dlv.order.number}",
                    body_ar=f"تم إدخال رمز استلام خاطئ {pin_tries} مرات",
                    body_en=f"{pin_tries} consecutive invalid delivery PIN attempts",
                    ref_type="order",
                    ref_id=dlv.order_id,
                )
            dlv.save(update_fields=["pin_attempts", "locked"])
            return {"ok": False, "left": left, "locked": bool(dlv.locked)}

        return {"ok": True}


def _resolve_delivered_quantities(lines: list[OrderLine], delivered_raw: Any) -> list[tuple[OrderLine, int, int]]:
    """Return list of (order_line, delivered_qty, remaining_qty)."""
    out: list[tuple[OrderLine, int, int]] = []
    if delivered_raw is None:
        return [(ln, ln.qty, 0) for ln in lines]

    if isinstance(delivered_raw, dict):
        # Match by order line id first, else unambiguous item_id
        item_counts: dict[int, int] = {}
        for ln in lines:
            item_counts[ln.item_id] = item_counts.get(ln.item_id, 0) + 1
        for ln in lines:
            val = delivered_raw.get(str(ln.id))
            if val is None and item_counts.get(ln.item_id, 0) == 1:
                val = delivered_raw.get(str(ln.item_id))
            if val is None:
                val = delivered_raw.get(ln.id)
            if val is None and item_counts.get(ln.item_id, 0) == 1:
                val = delivered_raw.get(ln.item_id)
            qty_del = ln.qty if val is None else int(val)
            if qty_del < 0 or qty_del > ln.qty:
                raise ApiException(code=ErrorCode.VALIDATION, message=("الكمية المسلمة غير صالحة", "Invalid delivered quantity"))
            out.append((ln, qty_del, ln.qty - qty_del))
        return out

    if isinstance(delivered_raw, list):
        by_line: dict[int, int] = {}
        by_item_unit: dict[tuple[int, str], int] = {}
        by_item: dict[int, int] = {}
        for entry in delivered_raw:
            if isinstance(entry, dict):
                qty_val = int(entry.get("qty", 0))
                if entry.get("line_id") is not None:
                    by_line[int(entry["line_id"])] = qty_val
                elif entry.get("item_id") is not None:
                    iid = int(entry["item_id"])
                    if entry.get("unit"):
                        by_item_unit[(iid, str(entry["unit"]))] = qty_val
                    by_item[iid] = qty_val
        for ln in lines:
            qty_del = by_line.get(ln.id, by_item_unit.get((ln.item_id, ln.unit), by_item.get(ln.item_id, ln.qty)))
            if qty_del < 0 or qty_del > ln.qty:
                raise ApiException(code=ErrorCode.VALIDATION, message=("الكمية المسلمة غير صالحة", "Invalid delivered quantity"))
            out.append((ln, qty_del, ln.qty - qty_del))
        return out

    raise ApiException(code=ErrorCode.VALIDATION, message=("صيغة الكميات المسلمة غير صالحة", "Invalid delivered quantities payload"))


def deliver_order(*, actor: User, order_id: int, data: dict[str, Any]) -> dict[str, Any]:
    pin_check = _check_delivery_pin_step(actor=actor, order_id=order_id, raw_pin=str(data.get("pin") or ""))
    if not pin_check["ok"]:
        return pin_check

    rules = rules_of()
    pay_raw = data.get("payment")
    pay_method: str | None = None
    pay_amt: int = 0
    if pay_raw is not None:
        if not isinstance(pay_raw, dict):
            msg = ("بيانات الدفع غير صالحة", "Invalid payment payload")
            raise ApiException(code=ErrorCode.VALIDATION, message=msg, status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY, errors={"payment": msg})
        pay_method = str(pay_raw.get("method") or "").strip()
        if pay_method not in DOOR_METHODS_ALL:
            msg = ("طريقة الدفع غير صالحة", "Invalid payment method")
            raise ApiException(code=ErrorCode.VALIDATION, message=msg, status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY, errors={"payment": msg})
        allowed_door = rules.get("door_methods") or list(DOOR_METHODS_ALL)
        if pay_method not in allowed_door or (pay_method in ("qi", "zaincash") and not rules["features"]["online_pay"]):
            raise ApiException(
                code=ErrorCode.FEATURE_OFF,
                message=("طريقة الدفع هذه موقفة", "That payment method is off"),
                status_code=http_status.HTTP_403_FORBIDDEN,
                errors={"payment": ("طريقة الدفع موقفة", "Payment method is off")},
            )
        raw_amt = pay_raw.get("amount")
        if isinstance(raw_amt, bool) or raw_amt in (None, ""):
            msg = ("المبلغ يجب أن يكون موجباً", "Amount must be positive")
            raise ApiException(code=ErrorCode.VALIDATION, message=msg, status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY, errors={"payment": msg})
        try:
            pay_amt = int(raw_amt)
            if pay_amt <= 0:
                raise ValueError
        except (TypeError, ValueError) as exc:
            msg = ("المبلغ يجب أن يكون موجباً", "Amount must be positive")
            raise ApiException(code=ErrorCode.VALIDATION, message=msg, status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY, errors={"payment": msg}) from exc
        if pay_method == "zaincash" and pay_amt < 1000:
            msg = ("زين كاش يقبل الدفع من 1,000 د.ع فأكثر", "ZainCash starts from 1,000 IQD")
            raise ApiException(code=ErrorCode.VALIDATION, message=msg, status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY, errors={"payment": msg})

    photo_required = bool(rules.get("photo_required", True))
    photo_raw = data.get("photo")
    storage_key = ""
    bucket = settings.SUPABASE_BUCKET_DELIVERY_PROOFS
    if photo_raw:
        raw_bytes, ext, mime = decode_image_data_url(photo_raw, max_dim=1200)
        storage_key = f"order-{order_id}-{uuid4().hex[:12]}.{ext}"
        get_storage().put_bytes(bucket=bucket, key=storage_key, content=raw_bytes, content_type=mime)
    elif photo_required:
        msg = ("صورة إثبات التسليم مطلوبة", "Delivery proof photo is required")
        raise ApiException(code=ErrorCode.VALIDATION, message=msg, errors={"photo": msg})

    rest_mode = str(data.get("rest") or "cancel").strip().lower()
    if rest_mode not in ("keep", "cancel"):
        raise ApiException(code=ErrorCode.VALIDATION, message=("خيار المتبقي غير صالح", "Invalid remainder option"))

    try:
        with transaction.atomic():
            dlv = Delivery.objects.select_for_update().select_related("order", "order__customer").get(order_id=order_id)
            order = Order.objects.select_for_update().get(id=order_id)
            if dlv.status != DeliveryStatus.OUT or order.status == OrderStatus.CONFIRMED:
                raise ApiException(code=ErrorCode.BAD_STATE, message=("تم تسليم الشحنة مسبقاً", "Order already delivered"))

            lines = list(OrderLine.objects.select_related("item").filter(order=order).order_by("id"))
            delivered_input = data.get("lines") if data.get("lines") is not None else data.get("delivered")
            split = _resolve_delivered_quantities(lines, delivered_input)

            delivered_specs: list[dict[str, Any]] = []
            remaining_specs: list[dict[str, Any]] = []
            for ln, del_qty, rem_qty in split:
                ppc = ln.item.pieces_per_carton
                if del_qty > 0:
                    del_pcs = del_qty * ppc if ln.unit == "carton" else del_qty
                    delivered_specs.append(
                        {
                            "order_line": ln,
                            "warehouse_id": ln.warehouse_id or order.warehouse_id,
                            "item_id": ln.item_id,
                            "unit": ln.unit,
                            "qty": del_qty,
                            "pieces": del_pcs,
                            "unit_price": ln.unit_price,
                            "total": del_qty * ln.unit_price,
                        }
                    )
                if rem_qty > 0:
                    rem_pcs = rem_qty * ppc if ln.unit == "carton" else rem_qty
                    remaining_specs.append(
                        {
                            "warehouse_id": ln.warehouse_id or order.warehouse_id,
                            "item_id": ln.item_id,
                            "unit": ln.unit,
                            "qty": rem_qty,
                            "pieces": rem_pcs,
                            "unit_price": ln.unit_price,
                            "total": rem_qty * ln.unit_price,
                        }
                    )

            if not delivered_specs:
                raise ApiException(code=ErrorCode.VALIDATION, message=("أدخل كمية واحدة مسلمة على الأقل", "At least one delivered quantity is required"))

            now = datetime.now(tz=UTC)
            inv = create_invoice_for_order(
                order=order,
                delivered_line_specs=delivered_specs,
                delivery_rep=dlv.rep or actor,
                actor=actor,
                delivered_at=now,
            )

            rest_order = None
            if remaining_specs and rest_mode == "keep":
                sub = sum(sp["total"] for sp in remaining_specs)
                disc_pct = order.discount_percent
                disc_amt = calc_percent_round(sub, disc_pct)
                after = max(0, sub - disc_amt)
                tot = round_amount_by_rule(after)
                rnd = tot - after
                res_hours = SystemSetting.get_solo().reservation_hours

                rest_order = Order.objects.create(
                    number=f"ORD-RST-{order.id}",
                    customer_id=order.customer_id,
                    warehouse_id=order.warehouse_id,
                    rep_id=order.rep_id,
                    created_by=actor,
                    source=order.source,
                    status=OrderStatus.APPROVED,
                    notes=order.notes,
                    subtotal=sub,
                    discount_percent=disc_pct,
                    discount_amount=disc_amt,
                    rounding=rnd,
                    total=tot,
                    reserved_until=now + timedelta(hours=res_hours),
                    parent=order,
                )
                rest_order.number = f"ORD-{rest_order.id:05d}"
                rest_order.save(update_fields=["number"])
                for sp in remaining_specs:
                    OrderLine.objects.create(
                        order=rest_order,
                        item_id=sp["item_id"],
                        warehouse_id=sp["warehouse_id"],
                        unit=sp["unit"],
                        qty=sp["qty"],
                        pieces=sp["pieces"],
                        unit_price=sp["unit_price"],
                        total=sp["total"],
                    )

            order.status = OrderStatus.CONFIRMED
            order.reserved_until = None
            if remaining_specs:
                order.short_lines = remaining_specs
            if rest_order:
                order.rest_order = rest_order
            order.save(update_fields=["status", "reserved_until", "short_lines", "rest_order"])
            sync_shipments(order)

            door_payment_obj: dict[str, Any] | None = None
            if pay_method is not None:
                owed = customer_balance_map([order.customer_id]).get(order.customer_id, 0)
                if pay_amt > owed:
                    raise ApiException(
                        code=ErrorCode.VALIDATION,
                        message=("المبلغ أكبر من الدين", "Amount is more than the debt"),
                        status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
                        errors={
                            "payment": (
                                f"دين المحل مع هذه الفاتورة {owed:,} د.ع",
                                f"The shop's debt with this invoice is {owed:,} IQD",
                            )
                        },
                    )
                if pay_method == "cash":
                    door_cref = f"door:{order.id}"
                    pay_rec = Payment.objects.filter(client_ref=door_cref).first()
                    if not pay_rec:
                        pay_rec = _allocate_payment_fifo(
                            customer=order.customer,
                            amount=pay_amt,
                            method=PaymentMethod.CASH,
                            note=f"نقداً عند تسليم {order.number}",
                            received_by=actor,
                            received_by_name=actor.name,
                            received_by_role=actor.role,
                            client_ref=door_cref,
                        )
                        if actor.role == Role.REP:
                            check_rep_custody_cap(actor)
                    door_payment_obj = {
                        "method": "cash",
                        "amount": pay_amt,
                        "status": "paid",
                        "at": now.isoformat(),
                        "payment_id": pay_rec.id,
                        "paid_amount": pay_rec.amount,
                        "paid_at": now.isoformat(),
                    }
                else:
                    door_payment_obj = {
                        "method": pay_method,
                        "amount": pay_amt,
                        "status": "requested",
                        "at": now.isoformat(),
                    }

            dlv.status = DeliveryStatus.DELIVERED
            dlv.delivered_by = actor
            dlv.photo_path = storage_key
            dlv.payment = door_payment_obj
            dlv.delivered_at = now
            dlv.problem_reason = ""
            dlv.problem_note = ""
            dlv.problem_by = None
            dlv.problem_at = None
            dlv.save(
                update_fields=[
                    "status",
                    "delivered_by",
                    "photo_path",
                    "payment",
                    "delivered_at",
                    "problem_reason",
                    "problem_note",
                    "problem_by",
                    "problem_at",
                ]
            )

            notify_customer(
                customer_id=order.customer_id,
                kind="delivered",
                title_ar=f"تم تسليم الطلب {order.number}",
                title_en=f"Order {order.number} delivered",
                body_ar=f"صدرت الفاتورة {inv.number} بمبلغ {inv.total:,} د.ع",
                body_en=f"Invoice {inv.number} issued for {inv.total:,} IQD",
                ref_type="invoice",
                ref_id=inv.id,
            )
            record_audit(
                actor=actor,
                action="confirm",
                entity_type="order",
                entity_id=order.id,
                summary=f"Delivered order {order.number} -> invoice {inv.number}",
                summary_ar=f"تسليم الطلب {order.number} وإصدار الفاتورة {inv.number}",
                after={"invoice_id": inv.id, "rest_order_id": rest_order.id if rest_order else None},
            )
            publish_order_changed(order.id)
            if order.shipment_of_id:
                publish_order_changed(order.shipment_of_id)
            if rest_order is not None:
                publish_order_changed(rest_order.id)
    except Exception:
        if storage_key:
            get_storage().delete(bucket=bucket, key=storage_key)
        raise

    return {"ok": True, "order": get_order_for_actor(actor=actor, order_id=order_id)}


@transaction.atomic
def unlock_delivery(*, actor: User, order_id: int) -> dict[str, Any]:
    if actor.role != Role.MANAGER:
        raise ApiException(code=ErrorCode.FORBIDDEN, status_code=http_status.HTTP_403_FORBIDDEN)
    dlv = Delivery.objects.select_for_update().filter(order_id=order_id).first()
    if not dlv:
        raise ApiException(code=ErrorCode.NOT_FOUND, status_code=http_status.HTTP_404_NOT_FOUND)
    dlv.pin_attempts = 0
    dlv.locked = False
    dlv.save(update_fields=["pin_attempts", "locked"])
    record_audit(actor=actor, action="update", entity_type="order", entity_id=order_id, summary=f"Unlocked delivery PIN for order #{order_id}")
    publish_order_changed(order_id)
    return get_order_for_actor(actor=actor, order_id=order_id)


@transaction.atomic
def report_delivery_problem(*, actor: User, data: dict[str, Any]) -> list[dict[str, Any]]:
    if not (actor.role == Role.MANAGER or (actor.role == Role.REP and rep_may_deliver(actor))):
        raise ApiException(code=ErrorCode.FORBIDDEN, status_code=http_status.HTTP_403_FORBIDDEN)

    order_ids = data.get("order_ids")
    if not isinstance(order_ids, list) or not order_ids:
        raise ApiException(code=ErrorCode.VALIDATION, message=("اختر شحنة واحدة على الأقل", "Select at least one shipment"))
    reason = str(data.get("reason") or "").strip().lower()
    if reason not in VALID_PROBLEM_REASONS:
        raise ApiException(code=ErrorCode.VALIDATION, message=("اختر سبب التعذر", "Select a valid problem reason"))
    note = (str(data.get("note") or "")).strip()
    if reason == "other" and not note:
        raise ApiException(code=ErrorCode.VALIDATION, message=("أدخل ملاحظة توضيحية", "Note is required for other reason"), errors={"note": ("أدخل ملاحظة توضيحية", "Note is required")})

    dlvs = list(Delivery.objects.select_for_update().select_related("order").filter(order_id__in=order_ids).order_by("id"))
    if len(dlvs) != len(set(int(x) for x in order_ids)):
        raise ApiException(code=ErrorCode.NOT_FOUND, status_code=http_status.HTTP_404_NOT_FOUND)

    now = datetime.now(tz=UTC)
    updated_orders = []
    for dlv in dlvs:
        if actor.role != Role.MANAGER and dlv.rep_id != actor.id:
            raise ApiException(code=ErrorCode.FORBIDDEN, status_code=http_status.HTTP_403_FORBIDDEN)
        if dlv.status != DeliveryStatus.OUT:
            raise ApiException(code=ErrorCode.BAD_STATE, message=("الشحنة ليست قيد التوصيل", "Shipment is not out for delivery"))
        dlv.problem_reason = reason
        dlv.problem_note = note
        dlv.problem_by = actor
        dlv.problem_at = now
        dlv.save(update_fields=["problem_reason", "problem_note", "problem_by", "problem_at"])

        notify_role(
            role=Role.MANAGER,
            kind="delivery_problem",
            title_ar=f"مشكلة في تسليم {dlv.order.number}",
            title_en=f"Delivery problem on {dlv.order.number}",
            body_ar=note or reason,
            body_en=note or reason,
            ref_type="order",
            ref_id=dlv.order_id,
        )
        record_audit(
            actor=actor,
            action="update",
            entity_type="order",
            entity_id=dlv.order_id,
            summary=f"Delivery problem ({reason}) reported on {dlv.order.number}",
            summary_ar=f"إبلاغ عن تعذر تسليم الشحنة {dlv.order.number} ({reason})",
            after={"reason": reason, "note": note},
        )
        publish_order_changed(dlv.order_id)
        updated_orders.append(get_order_for_actor(actor=actor, order_id=dlv.order_id))

    return updated_orders


@transaction.atomic
def return_delivery_to_warehouse(*, actor: User, order_id: int) -> dict[str, Any]:
    if actor.role not in (Role.MANAGER, Role.WAREHOUSE):
        raise ApiException(code=ErrorCode.FORBIDDEN, status_code=http_status.HTTP_403_FORBIDDEN)

    dlv = Delivery.objects.select_for_update().select_related("order").filter(order_id=order_id).first()
    if not dlv:
        raise ApiException(code=ErrorCode.NOT_FOUND, status_code=http_status.HTTP_404_NOT_FOUND)

    if actor.role in (Role.WAREHOUSE, Role.STAFF) and not works_in_warehouse(actor, dlv.order.warehouse_id):
        if not any(works_in_warehouse(actor, wid) for wid in dlv.order.prep_parts.values_list("warehouse_id", flat=True)):
            raise ApiException(code=ErrorCode.FORBIDDEN, status_code=http_status.HTTP_403_FORBIDDEN)

    if dlv.status != DeliveryStatus.OUT:
        raise ApiException(code=ErrorCode.BAD_STATE, message=("الشحنة ليست خارج المخزن حالياً", "Shipment is not currently out"))

    dlv.status = DeliveryStatus.READY
    dlv.rep = None
    dlv.dispatched_by = None
    dlv.problem_reason = ""
    dlv.problem_note = ""
    dlv.problem_by = None
    dlv.problem_at = None
    dlv.dispatched_at = None
    dlv.save(
        update_fields=[
            "status",
            "rep",
            "dispatched_by",
            "problem_reason",
            "problem_note",
            "problem_by",
            "problem_at",
            "dispatched_at",
        ]
    )

    record_audit(
        actor=actor,
        action="update",
        entity_type="order",
        entity_id=order_id,
        summary=f"Returned undelivered shipment {dlv.order.number} to warehouse",
        summary_ar=f"إرجاع الشحنة {dlv.order.number} إلى المخزن",
    )
    publish_order_changed(order_id)
    return get_order_for_actor(actor=actor, order_id=order_id)


def get_delivery_photo_url(*, actor: User, order_id: int, request: Any = None) -> str | None:
    dlv = Delivery.objects.select_related("order", "order__customer").filter(order_id=order_id).first()
    if not dlv:
        raise ApiException(code=ErrorCode.NOT_FOUND, status_code=http_status.HTTP_404_NOT_FOUND)
    if not can_see_order(actor, dlv.order):
        raise ApiException(code=ErrorCode.FORBIDDEN, status_code=http_status.HTTP_403_FORBIDDEN)
    if not dlv.photo_path:
        return None
    return get_storage().get_url(
        bucket=settings.SUPABASE_BUCKET_DELIVERY_PROOFS,
        key=dlv.photo_path,
        request=request,
        private=True,
        expires_in=int(getattr(settings, "SIGNED_URL_TTL_SECONDS", 900)),
    )


def rep_has_active_delivery(rep_id: int) -> bool:
    return Delivery.objects.filter(status=DeliveryStatus.OUT).filter(
        Q(rep_id=rep_id) | Q(rep__isnull=True, order__rep_id=rep_id)
    ).exists()


@transaction.atomic
def record_rep_location(
    *,
    actor: User,
    lat: Any,
    lng: Any,
    emit_realtime: bool = True,
) -> tuple[dict[str, Any], bool]:
    if actor.role != Role.REP or not rep_may_deliver(actor):
        raise ApiException(code=ErrorCode.FORBIDDEN, status_code=http_status.HTTP_403_FORBIDDEN)
    try:
        lat_f = float(lat)
        lng_f = float(lng)
        if not (math.isfinite(lat_f) and math.isfinite(lng_f)) or abs(lat_f) > 90 or abs(lng_f) > 180:
            raise ValueError
    except (TypeError, ValueError) as exc:
        raise ApiException(
            code=ErrorCode.VALIDATION,
            message=("موقع غير صحيح", "Not a valid location"),
            errors={"lat": ("موقع غير صحيح", "Invalid coordinates")},
        ) from exc

    if not rep_has_active_delivery(actor.id):
        raise ApiException(
            code=ErrorCode.BAD_STATE,
            message=("لا توجد شحنة قيد التوصيل حالياً لمشاركة الموقع", "No active shipment out for delivery"),
        )

    lat_dec = Decimal(str(round(lat_f, 6))).quantize(Decimal("0.000001"))
    lng_dec = Decimal(str(round(lng_f, 6))).quantize(Decimal("0.000001"))
    now = datetime.now(tz=UTC)

    existing = RepLocation.objects.select_for_update().filter(rep=actor).first()
    should_broadcast = True
    if existing:
        if (
            existing.lat == lat_dec
            and existing.lng == lng_dec
            and (now - existing.updated_at) < timedelta(seconds=120)
        ):
            should_broadcast = False
            loc = existing
        else:
            existing.lat = lat_dec
            existing.lng = lng_dec
            existing.save(update_fields=["lat", "lng", "updated_at"])
            loc = existing
    else:
        loc = RepLocation.objects.create(rep=actor, lat=lat_dec, lng=lng_dec)

    at_iso = loc.updated_at.isoformat()
    lat_out = float(loc.lat)
    lng_out = float(loc.lng)
    if emit_realtime and should_broadcast:
        publish_rep_location(rep_id=actor.id, lat=lat_out, lng=lng_out, at=at_iso)
    return (
        {
            "rep_id": actor.id,
            "lat": lat_out,
            "lng": lng_out,
            "at": at_iso,
        },
        should_broadcast,
    )


def update_rep_location(*, actor: User, lat: Any, lng: Any) -> dict[str, Any]:
    payload, _ = record_rep_location(actor=actor, lat=lat, lng=lng, emit_realtime=True)
    return payload


def list_rep_locations(*, actor: User) -> list[dict[str, Any]]:
    cutoff = datetime.now(tz=UTC) - timedelta(minutes=10)
    qs = RepLocation.objects.filter(updated_at__gte=cutoff).order_by("rep_id")
    if actor.role == Role.SHOP:
        cid = get_active_customer_id(actor)
        if not cid:
            return []
        active_dlvs = Delivery.objects.select_related("order").filter(
            status=DeliveryStatus.OUT,
            order__customer_id=cid,
        )
        rep_ids = {d.rep_id or d.order.rep_id for d in active_dlvs if (d.rep_id or d.order.rep_id)}
        if not rep_ids:
            return []
        qs = qs.filter(rep_id__in=rep_ids)
    elif actor.role == Role.REP:
        qs = qs.filter(rep_id=actor.id)
    elif actor.role in (Role.WAREHOUSE, Role.STAFF):
        own_wh = own_warehouse_id(actor)
        if own_wh is not None:
            wh_dlvs = Delivery.objects.select_related("order").filter(
                status__in=[DeliveryStatus.OUT, DeliveryStatus.PROBLEM],
            ).filter(Q(order__warehouse_id=own_wh) | Q(order__lines__warehouse_id=own_wh))
            rep_ids = {d.rep_id or d.order.rep_id for d in wh_dlvs if (d.rep_id or d.order.rep_id)}
            if not rep_ids:
                return []
            qs = qs.filter(rep_id__in=rep_ids)

    return [
        {
            "rep_id": r.rep_id,
            "lat": float(r.lat),
            "lng": float(r.lng),
            "at": r.updated_at.isoformat(),
        }
        for r in qs
    ]


def lines_weight_kg(lines: list[Any]) -> float:
    """Compute total weight in kg (rounded to 1 decimal place) for iterable of OrderLines with .item & .pieces."""
    total = 0.0
    for line in lines:
        item = getattr(line, "item", None)
        if not item:
            continue
        pieces = int(getattr(line, "pieces", 0) or 0)
        total += pieces * item_piece_weight_kg(item)
    return round(total * 10.0) / 10.0


def order_weight_kg(order: Order) -> float:
    return lines_weight_kg(list(order.lines.select_related("item").all()))


def vehicle_load_kg(vehicle_id: int, *, exclude_order_id: int | None = None) -> float:
    """Current weight (kg) inside a vehicle: shipments loaded at warehouse or currently out for delivery."""
    dlvs = (
        Delivery.objects.select_related("order")
        .prefetch_related("order__lines__item")
        .filter(vehicle_id=vehicle_id)
        .filter(Q(status=DeliveryStatus.OUT) | Q(status=DeliveryStatus.READY, loaded_at__isnull=False))
        .exclude(order__status__in=[OrderStatus.CANCELLED, OrderStatus.EXPIRED])
    )
    if exclude_order_id is not None:
        dlvs = dlvs.exclude(order_id=exclude_order_id)
    return round(sum(lines_weight_kg(list(d.order.lines.all())) for d in dlvs) * 10.0) / 10.0


def sync_shipments(part: Order) -> None:
    """When a child shipment finishes (confirmed/cancelled), update parent split order status if all children finished."""
    if not part.shipment_of_id:
        return
    parent = Order.objects.select_for_update().filter(id=part.shipment_of_id).first()
    if not parent:
        return
    children = list(Order.objects.filter(shipment_of_id=parent.id).only("id", "status"))
    if not children:
        return
    if all(ch.status in (OrderStatus.CONFIRMED, OrderStatus.CANCELLED) for ch in children):
        new_status = (
            OrderStatus.CONFIRMED
            if any(ch.status == OrderStatus.CONFIRMED for ch in children)
            else OrderStatus.CANCELLED
        )
        if parent.status != new_status:
            parent.status = new_status
            parent.reserved_until = None
            parent.save(update_fields=["status", "reserved_until"])


@transaction.atomic
def save_vehicle(*, actor: User, vehicle_id: int | None = None, data: dict[str, Any]) -> dict[str, Any]:
    if actor.role != Role.MANAGER:
        raise ApiException(code=ErrorCode.FORBIDDEN, status_code=http_status.HTTP_403_FORBIDDEN)

    name = str(data.get("name") or "").strip()
    plate = str(data.get("plate") or "").strip()
    raw_cap = data.get("capacity_kg")
    raw_driver_id = data.get("driver_id")
    active = bool(data.get("active", True))

    errors: dict[str, tuple[str, str] | str] = {}
    if not name:
        errors["name"] = ("الاسم مطلوب", "Name is required")

    cap_val: int | None = None
    try:
        cap_num = float(raw_cap)
        if cap_num < 50 or cap_num > 40000:
            raise ValueError("out of range")
        cap_val = int(round(cap_num))
    except (TypeError, ValueError):
        errors["capacity_kg"] = ("بين 50 و 40,000 كغم", "Between 50 and 40,000 kg")

    driver: User | None = None
    if raw_driver_id not in (None, "", 0, "0"):
        try:
            did = int(raw_driver_id)
        except (TypeError, ValueError):
            did = 0
        driver = User.objects.filter(id=did, role=Role.REP, deleted=False).first() if did > 0 else None
        if driver is None:
            errors["driver_id"] = ("اختر مندوب توصيل", "Choose a delivery rep")

    if errors:
        raise ApiException(
            code=ErrorCode.VALIDATION,
            message=("تحقق من الحقول", "Check the fields"),
            errors=errors,
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
        )

    assert cap_val is not None
    if vehicle_id is not None:
        v = Vehicle.objects.select_for_update().select_related("driver").filter(id=vehicle_id).first()
        if not v:
            raise ApiException(code=ErrorCode.NOT_FOUND, status_code=http_status.HTTP_404_NOT_FOUND)
        v.name = name
        v.plate = plate
        v.capacity_kg = cap_val
        v.driver = driver
        v.active = active
        v.save(update_fields=["name", "plate", "capacity_kg", "driver", "active"])
        record_audit(
            actor=actor,
            action="update",
            entity_type="settings",
            entity_id=v.id,
            summary=f"Vehicle {v.name}: {v.capacity_kg} kg",
            summary_ar=f"تعديل السيارة {v.name}: حمولة {v.capacity_kg} كغم",
        )
        return serialize_vehicle(v)

    v = Vehicle.objects.create(
        name=name,
        plate=plate,
        capacity_kg=cap_val,
        driver=driver,
        active=active,
    )
    record_audit(
        actor=actor,
        action="create",
        entity_type="settings",
        entity_id=v.id,
        summary=f"New vehicle {v.name}: {v.capacity_kg} kg",
        summary_ar=f"إضافة السيارة {v.name}: حمولة {v.capacity_kg} كغم",
    )
    return serialize_vehicle(v)


@transaction.atomic
def load_shipment(*, actor: User, order_id: int) -> dict[str, Any]:
    if actor.role not in (Role.STAFF, Role.WAREHOUSE, Role.MANAGER):
        raise ApiException(code=ErrorCode.FORBIDDEN, status_code=http_status.HTTP_403_FORBIDDEN)

    order = Order.objects.select_for_update().select_related("customer").filter(id=order_id).first()
    if not order:
        raise ApiException(code=ErrorCode.NOT_FOUND, status_code=http_status.HTTP_404_NOT_FOUND)

    dlv = Delivery.objects.select_for_update().select_related("vehicle", "rep").filter(order_id=order_id).first()
    if not dlv:
        raise ApiException(
            code=ErrorCode.BAD_STATE,
            message=("الطلب لم يُجهَّز بعد", "The order isn't prepared yet"),
            status_code=http_status.HTTP_409_CONFLICT,
        )

    if actor.role in (Role.STAFF, Role.WAREHOUSE) and not works_in_warehouse(actor, order.warehouse_id):
        if not any(works_in_warehouse(actor, wid) for wid in order.prep_parts.values_list("warehouse_id", flat=True)):
            raise ApiException(
                code=ErrorCode.FORBIDDEN,
                message=("هذه الشحنة من مخزن آخر", "This shipment is from another warehouse"),
                status_code=http_status.HTTP_403_FORBIDDEN,
            )

    if dlv.status != DeliveryStatus.READY:
        raise ApiException(
            code=ErrorCode.BAD_STATE,
            message=("خرجت الشحنة من المخزن", "The shipment already left"),
            status_code=http_status.HTTP_409_CONFLICT,
        )

    if dlv.loaded_at is not None:
        return get_order_for_actor(actor=actor, order_id=order_id)

    if dlv.vehicle is not None:
        v = dlv.vehicle
        inside = vehicle_load_kg(v.id, exclude_order_id=order.id)
        kg = order_weight_kg(order)
        if inside + kg > v.capacity_kg:
            candidates = [
                (x.capacity_kg - vehicle_load_kg(x.id), x)
                for x in Vehicle.objects.filter(active=True).exclude(id=v.id).order_by("id")
            ]
            fitting = [pair for pair in candidates if pair[0] >= kg]
            fitting.sort(key=lambda pair: -pair[0])
            other = fitting[0][1] if fitting else None
            ar_hint = f" اقتراح: {other.name}." if other else ""
            en_hint = f" Try {other.name}." if other else ""
            raise ApiException(
                code=ErrorCode.BAD_STATE,
                message=(
                    f"{v.name} امتلأت: فيها {int(round(inside))} كغم وهذه الشحنة {int(round(kg))} كغم (الحمولة {v.capacity_kg} كغم).{ar_hint} غيّر السيارة من صفحة الطلب.",
                    f"{v.name} is full: {int(round(inside))} kg in it plus {int(round(kg))} kg (max {v.capacity_kg} kg).{en_hint}",
                ),
                status_code=http_status.HTTP_409_CONFLICT,
            )

    now = datetime.now(tz=UTC)
    dlv.loaded_at = now
    dlv.loaded_by = actor
    dlv.save(update_fields=["loaded_at", "loaded_by"])

    customer_name = order.customer.name if order.customer else ""
    record_audit(
        actor=actor,
        action="update",
        entity_type="order",
        entity_id=order.id,
        summary=f"Shipment {order.number} loaded",
        summary_ar=f"تحميل الشحنة {order.number} ({customer_name}) في سيارة التوصيل",
    )
    if dlv.rep is not None:
        notify_user(
            user_id=dlv.rep_id,
            kind="out_for_delivery",
            title_ar=f"حُمّلت شحنة {customer_name} في سيارتك",
            title_en=f"{customer_name}'s shipment is in your van",
            body_ar=f"{order.number}: امسح ملصقها، ثم اختر محلات جولتك.",
            body_en=f"{order.number}: scan its label, then choose your trip.",
            ref_type="delivery",
            ref_id=order.id,
        )
    publish_order_changed(order.id)
    return get_order_for_actor(actor=actor, order_id=order_id)
