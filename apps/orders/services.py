"""Order creation, offline pricing & stock split, approval routing, and multi-warehouse preparation services."""
from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import uuid4

from django.apps import apps
from django.contrib.auth.hashers import make_password
from django.db import transaction
from django.db.models import Min, Q
from django.utils import timezone
from rest_framework import status as http_status

from apps.accounts.business_rules import round_amount_by_rule, rules_of
from apps.accounts.models import Role, SystemSetting, User
from apps.accounts.permissions import (
    covering_warehouse_ids,
    get_active_customer_id,
    own_warehouse_id,
    rep_may_sell,
    works_in_warehouse,
)
from apps.accounts.profiles import (
    batch_custom_values,
    custom_values_for_object,
    may_profile_action,
    validate_and_apply_custom_on_create,
)
from apps.accounts.rules_engine import run_automation_rules
from apps.audit.services import record_audit
from apps.catalog.models import Item, ItemPrice, PriceChange
from apps.common.exceptions import ApiException, ErrorCode
from apps.common.utils import calc_percent_round, decrypt_secret, encrypt_secret
from apps.customers.models import Customer, CustomerStatus, SpecialPrice
from apps.customers.services import customer_balance_map
from apps.deliveries.models import Delivery, DeliveryStatus, Vehicle
from apps.inventory.services import is_count_locked, shelf_available
from apps.notifications.services import notify_role, notify_user
from apps.orders.models import Order, OrderLine, OrderSource, OrderStatus, PrepPart
from apps.realtime.broadcaster import publish_order_changed
from apps.warehouses.models import Warehouse

DISCOUNT_MAX = Decimal("30")
STAFF_DISCOUNT_MAX = Decimal("3")
SHOP_MAX_QTY = 100
_CODE_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"


def sweep_expired_reservations() -> int:
    """Expire stale pending/needs_approval orders past reserved_until (excluding waiting_stock orders)."""
    now = datetime.now(tz=UTC)
    return Order.objects.filter(
        status__in=[OrderStatus.PENDING, OrderStatus.NEEDS_APPROVAL],
        reserved_until__isnull=False,
        reserved_until__lte=now,
    ).update(status=OrderStatus.EXPIRED)


def _generate_delivery_code() -> str:
    for _ in range(20):
        code = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(8))
        if not Delivery.objects.filter(code=code).exists():
            return code
    return uuid4().hex[:8].upper()


def _generate_delivery_pin() -> str:
    return f"{secrets.randbelow(9000) + 1000}"


def can_see_order(actor: User, order: Order) -> bool:
    if actor.role in (Role.MANAGER, Role.ACCOUNTANT):
        return True
    if actor.role in (Role.WAREHOUSE, Role.STAFF):
        if works_in_warehouse(actor, order.warehouse_id):
            return True
        if any(works_in_warehouse(actor, wid) for wid in order.lines.values_list("warehouse_id", flat=True)):
            return True
        if any(works_in_warehouse(actor, wid) for wid in order.prep_parts.values_list("warehouse_id", flat=True)):
            return True
        return False
    if actor.role == Role.REP:
        if order.rep_id == actor.id or (order.customer and order.customer.rep_id == actor.id):
            return True
        dlv = getattr(order, "delivery", None)
        if dlv and dlv.rep_id == actor.id:
            return True
        return False
    if actor.role == Role.SHOP:
        cid = get_active_customer_id(actor)
        return cid is not None and order.customer_id == cid
    return False


def serialize_delivery(delivery: Delivery, *, actor: User, order: Order) -> dict[str, Any]:
    # Confidentiality rule: ONLY the owning shop customer receives the plain 4-digit delivery PIN
    pin_out: str | None = None
    if actor.role == Role.SHOP and get_active_customer_id(actor) == order.customer_id:
        try:
            pin_out = decrypt_secret(delivery.pin_encrypted)
        except Exception:
            pin_out = None

    problem_obj = None
    if delivery.problem_reason:
        problem_obj = {
            "reason": delivery.problem_reason,
            "note": delivery.problem_note or "",
            "at": delivery.problem_at.isoformat() if delivery.problem_at else None,
            "by": {
                "id": delivery.problem_by_id or 0,
                "name": delivery.problem_by.name if delivery.problem_by else "",
            },
        }

    delivered_by_obj = None
    if delivery.delivered_by:
        delivered_by_obj = {"id": delivery.delivered_by_id, "name": delivery.delivered_by.name}
    elif delivery.delivered_at and delivery.rep:
        delivered_by_obj = {"id": delivery.rep_id, "name": delivery.rep.name}

    out_by_obj = None
    if delivery.dispatched_by:
        out_by_obj = {"id": delivery.dispatched_by_id, "name": delivery.dispatched_by.name}

    rep_obj = {"id": delivery.rep_id, "name": delivery.rep.name} if delivery.rep else None

    return {
        "code": delivery.code,
        "status": delivery.status,
        "rep_id": delivery.rep_id,
        "rep": rep_obj,
        "pin": pin_out,
        "attempts": delivery.pin_attempts,
        "pin_attempts": delivery.pin_attempts,
        "locked": bool(delivery.locked),
        "has_photo": bool(delivery.photo_path),
        "problem_reason": delivery.problem_reason,
        "problem": problem_obj,
        "out_by": out_by_obj,
        "out_at": (delivery.out_at or delivery.dispatched_at).isoformat() if (delivery.out_at or delivery.dispatched_at) else None,
        "out_phone": delivery.out_phone or (delivery.rep.phone if delivery.rep and delivery.status == "out" else ""),
        "delivered_by": delivered_by_obj,
        "dispatched_at": delivery.dispatched_at.isoformat() if delivery.dispatched_at else None,
        "delivered_at": delivery.delivered_at.isoformat() if delivery.delivered_at else None,
        **({"vehicle": {"id": delivery.vehicle_id, "name": delivery.vehicle.name}} if delivery.vehicle else {}),
        **({"loaded_at": delivery.loaded_at.isoformat()} if delivery.loaded_at else {}),
        **(
            {"loaded_by": {"id": delivery.loaded_by_id, "name": delivery.loaded_by.name}}
            if delivery.loaded_by
            else {}
        ),
        **({"payment": delivery.payment} if getattr(delivery, "payment", None) is not None else {}),
    }


def serialize_order(order: Order, *, actor: User, custom_values: dict[str, Any] | None = None) -> dict[str, Any]:
    pct = order.discount_percent
    pct_out: int | float = int(pct) if pct == int(pct) else float(pct)

    dlv = getattr(order, "delivery", None)
    inv = getattr(order, "invoice", None)

    discount_obj = None
    if order.discount_percent > 0 or order.discount_amount > 0:
        disc_actor = order.discount_by or order.created_by
        discount_obj = {
            "percent": pct_out,
            "amount": order.discount_amount,
            "by": {"id": disc_actor.id, "name": disc_actor.name, "role": disc_actor.role},
            "at": (order.discount_at or order.created_at).isoformat(),
        }
        if "discount" in (order.approval_for or []):
            discount_obj["needs_approval"] = True
        if order.approved_by:
            discount_obj["approved_by"] = {"id": order.approved_by_id, "name": order.approved_by.name}

    prep_parts_out = [
        {
            "warehouse_id": p.warehouse_id,
            **({"prepared_by": {"id": p.prepared_by_id, "name": p.prepared_by.name}} if p.prepared_by else {}),
            **({"at": p.prepared_at.isoformat()} if p.prepared_at else {}),
        }
        for p in order.prep_parts.select_related("prepared_by").order_by("id")
    ]

    eff_rep_id = (
        order.rep_id
        or (dlv.rep_id if dlv and dlv.rep_id else None)
        or (order.carrier_pick.get("rep_id") if isinstance(order.carrier_pick, dict) else None)
        or 0
    )

    return {
        "id": order.id,
        "number": order.number,
        "client_ref": order.client_ref or "",
        "customer_id": order.customer_id,
        "warehouse_id": order.warehouse_id,
        "rep_id": eff_rep_id,
        "source": order.source,
        "status": order.status,
        "waiting_stock": bool(order.waiting_stock),
        "approval_for": list(order.approval_for or []),
        "notes": order.notes,
        "note": order.notes or None,
        "subtotal": order.subtotal,
        "discount_percent": pct_out,
        "discount_amount": order.discount_amount,
        "discount": discount_obj,
        "rounding": order.rounding,
        "total": order.total,
        "reserved_until": order.reserved_until.isoformat() if order.reserved_until else None,
        "offline": bool(order.offline),
        "recorded_at": order.recorded_at.isoformat() if order.recorded_at else None,
        "parent_id": order.parent_id,
        "rest_order_id": order.rest_order_id,
        "cancel_reason": order.cancel_reason,
        "cancelled_at": order.cancelled_at.isoformat() if order.cancelled_at else None,
        "accepted_by": (
            {"id": order.accepted_by_id, "name": order.accepted_by.name}
            if order.accepted_by
            else ({"id": 0, "name": "اعتماد تلقائي"} if order.accepted_at else None)
        ),
        "accepted_at": order.accepted_at.isoformat() if order.accepted_at else None,
        "approved_by": {"id": order.approved_by_id, "name": order.approved_by.name} if order.approved_by else None,
        "approved_at": order.approved_at.isoformat() if order.approved_at else None,
        "edited_at": order.edited_at.isoformat() if order.edited_at else None,
        "created_at": order.created_at.isoformat(),
        "created_by": {"id": order.created_by_id, "name": order.created_by.name},
        "invoice_id": inv.id if inv else None,
        "lines": [
            {
                "id": ln.id,
                "item_id": ln.item_id,
                "warehouse_id": ln.warehouse_id or order.warehouse_id,
                "unit": ln.unit,
                "qty": ln.qty,
                "pieces": ln.pieces,
                "unit_price": ln.unit_price,
                "total": ln.total,
                **({"offer_off": ln.offer_off} if ln.offer_off else {}),
                **({"clearance": True} if getattr(ln, "clearance", False) else {}),
            }
            for ln in order.lines.order_by("id")
        ],
        **({"short_lines": list(order.short_lines)} if order.short_lines else {}),
        **({"prep_parts": prep_parts_out} if prep_parts_out else {}),
        "delivery": serialize_delivery(dlv, actor=actor, order=order) if dlv else None,
        **(
            {
                "shipments": [
                    ch.id
                    for ch in sorted(order.shipment_children.all(), key=lambda c: ((c.shipment_no or 0), c.id))
                ]
            }
            if order.shipment_children.exists()
            else {}
        ),
        **({"shipment_of": order.shipment_of_id} if order.shipment_of_id else {}),
        **({"shipment_no": order.shipment_no} if order.shipment_no is not None else {}),
        **({"rule_note": order.rule_note} if order.rule_note else {}),
        **({"counter": order.counter} if order.counter else {}),
        **({"rating": order.rating} if order.rating else {}),
        **({"carrier_pick": order.carrier_pick} if order.carrier_pick else {}),
        **({"custom": custom_values} if custom_values else {}),
    }


def _order_qs():
    rels = [
        "customer",
        "created_by",
        "discount_by",
        "accepted_by",
        "approved_by",
        "delivery",
        "delivery__rep",
        "delivery__dispatched_by",
        "delivery__delivered_by",
        "delivery__problem_by",
        "delivery__vehicle",
        "delivery__loaded_by",
    ]
    if apps.is_installed("apps.invoices"):
        rels.append("invoice")
    return Order.objects.select_related(*rels).prefetch_related("lines", "prep_parts__prepared_by", "shipment_children")


def list_orders_for_actor(*, actor: User) -> list[dict[str, Any]]:
    sweep_expired_reservations()
    qs = _order_qs().order_by("-id")
    own_wh = own_warehouse_id(actor)
    cov_whs = covering_warehouse_ids(actor)
    if own_wh is not None or cov_whs:
        wh_ids = [w for w in [own_wh, *cov_whs] if w is not None]
        qs = qs.filter(
            Q(warehouse_id__in=wh_ids) | Q(lines__warehouse_id__in=wh_ids) | Q(prep_parts__warehouse_id__in=wh_ids)
        ).distinct()
    elif actor.role == Role.REP:
        qs = qs.filter(Q(rep_id=actor.id) | Q(customer__rep_id=actor.id) | Q(delivery__rep_id=actor.id)).distinct()
    elif actor.role == Role.SHOP:
        cid = get_active_customer_id(actor)
        qs = qs.filter(customer_id=cid) if cid else qs.none()

    orders = list(qs)
    cmap = batch_custom_values("order", [o.id for o in orders], actor)
    return [serialize_order(o, actor=actor, custom_values=cmap.get(o.id)) for o in orders]


def get_order_for_actor(*, actor: User, order_id: int) -> dict[str, Any]:
    sweep_expired_reservations()
    order = _order_qs().filter(id=order_id).first()
    if not order:
        raise ApiException(code=ErrorCode.NOT_FOUND, status_code=http_status.HTTP_404_NOT_FOUND)
    if not can_see_order(actor, order):
        raise ApiException(code=ErrorCode.FORBIDDEN, status_code=http_status.HTTP_403_FORBIDDEN)
    return serialize_order(order, actor=actor, custom_values=custom_values_for_object("order", order.id, actor))


def _resolve_unit_price(item: Item, customer: Customer, unit: str, price_at: datetime | None) -> int:
    sp = SpecialPrice.objects.filter(customer=customer, item=item).first()
    if sp:
        base = sp.carton_price if unit == "carton" else sp.piece_price
    elif price_at is not None:
        hist = (
            PriceChange.objects.filter(item=item, tier_id=customer.tier_id, effective_from__lte=price_at)
            .order_by("-effective_from", "-id")
            .first()
        )
        if hist:
            base = hist.carton_price if unit == "carton" else hist.piece_price
        else:
            ip = ItemPrice.objects.filter(item=item, tier_id=customer.tier_id).first() or ItemPrice.objects.filter(item=item).order_by("tier_id").first()
            base = (ip.carton_price if unit == "carton" else ip.piece_price) if ip else 0
    else:
        ip = ItemPrice.objects.filter(item=item, tier_id=customer.tier_id).first() or ItemPrice.objects.filter(item=item).order_by("tier_id").first()
        base = (ip.carton_price if unit == "carton" else ip.piece_price) if ip else 0

    if price_at is None and base > 0:
        from apps.catalog.features_services import active_clearance_map

        cl = active_clearance_map([item.id]).get(item.id)
        if cl:
            min_step = max(1, int(rules_of().get("round_to") or 1))
            return max(min_step, round_amount_by_rule(int((Decimal(base) * Decimal(100 - cl["percent"]) / Decimal(100)).quantize(Decimal("1")))))
    return base


def _committed_order_debt(customer_id: int, exclude_order_id: int | None = None) -> int:
    now = datetime.now(tz=UTC)
    qs = Order.objects.filter(
        customer_id=customer_id,
    ).filter(
        Q(status=OrderStatus.APPROVED) | Q(status__in=[OrderStatus.PENDING, OrderStatus.NEEDS_APPROVAL], reserved_until__gt=now)
    )
    if exclude_order_id is not None:
        qs = qs.exclude(id=exclude_order_id)
    # Exclude orders already converted to invoices (invoice already reflects in customer_balance)
    if apps.is_installed("apps.invoices"):
        qs = qs.filter(invoice__isnull=True)
    return sum(o.total for o in qs.only("total"))


def _compute_totals(lines_specs: list[dict[str, Any]], discount_pct: Decimal) -> tuple[int, int, int, int]:
    subtotal = sum(ln["total"] for ln in lines_specs)
    disc_amt = calc_percent_round(subtotal, discount_pct)
    after = max(0, subtotal - disc_amt)
    total = round_amount_by_rule(after)
    rounding = total - after
    return subtotal, disc_amt, rounding, total


def _parse_discount_percent(raw: Any, actor_role: str, rules: dict[str, Any] | None = None) -> Decimal:
    if actor_role in (Role.SHOP, Role.WAREHOUSE, Role.STAFF):
        return Decimal("0")
    if raw in (None, ""):
        return Decimal("0")
    max_pct = int((rules or rules_of()).get("discount_max", int(DISCOUNT_MAX)))
    try:
        d = Decimal(str(raw))
        if d < 0 or d > Decimal(max_pct):
            raise ValueError
        return d
    except (InvalidOperation, ValueError) as exc:
        raise ApiException(
            code=ErrorCode.VALIDATION,
            message=(f"نسبة الخصم يجب أن تكون بين 0% و {max_pct}%", f"Discount percent must be between 0% and {max_pct}%"),
        ) from exc


@transaction.atomic
def create_order(*, actor: User, data: dict[str, Any]) -> dict[str, Any]:
    if actor.role not in (Role.MANAGER, Role.ACCOUNTANT, Role.REP, Role.SHOP):
        raise ApiException(code=ErrorCode.FORBIDDEN, status_code=http_status.HTTP_403_FORBIDDEN)

    client_ref = (str(data.get("client_ref") or "")).strip() or None
    if client_ref:
        existing = _order_qs().filter(client_ref=client_ref).first()
        if existing:
            return serialize_order(existing, actor=actor)

    rules = rules_of()
    if actor.role == Role.SHOP and rules["maintenance"]["on"]:
        maint = rules["maintenance"]
        raise ApiException(
            code=ErrorCode.MAINTENANCE,
            message=(
                maint.get("message") or "التطبيق في صيانة الآن",
                maint.get("message_en") or "The app is under maintenance",
            ),
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    sweep_expired_reservations()

    if actor.role == Role.REP and not rep_may_sell(actor):
        raise ApiException(
            code=ErrorCode.FORBIDDEN,
            message=("مندوب التوصيل لا يمكنه إنشاء طلبات بيع", "Delivery-only rep cannot place sales orders"),
            status_code=http_status.HTTP_403_FORBIDDEN,
        )

    if actor.role == Role.SHOP:
        cid = get_active_customer_id(actor)
        customer = Customer.objects.select_for_update().filter(id=cid).first() if cid else None
    else:
        customer = Customer.objects.select_for_update().filter(id=data.get("customer_id")).first()

    if not customer:
        raise ApiException(code=ErrorCode.NOT_FOUND, message=("الزبون غير موجود", "Customer not found"), status_code=http_status.HTTP_404_NOT_FOUND)
    if customer.status == CustomerStatus.PENDING or not customer.active:
        raise ApiException(
            code=ErrorCode.FORBIDDEN,
            message=("حساب الزبون غير مفعل أو بانتظار الموافقة", "Customer account is inactive or pending approval"),
            status_code=http_status.HTTP_403_FORBIDDEN,
        )
    if actor.role == Role.REP and customer.rep_id != actor.id:
        raise ApiException(code=ErrorCode.FORBIDDEN, status_code=http_status.HTTP_403_FORBIDDEN)

    wh_id = data.get("warehouse_id")
    wh = Warehouse.objects.filter(id=wh_id, active=True).first() if wh_id else Warehouse.objects.filter(active=True).order_by("id").first()
    if not wh:
        raise ApiException(code=ErrorCode.VALIDATION, message=("المخزن غير موجود", "Warehouse not found"))

    raw_lines = data.get("lines")
    if not isinstance(raw_lines, list) or not raw_lines:
        raise ApiException(code=ErrorCode.VALIDATION, message=("أضف مادة واحدة على الأقل", "At least one order line is required"))

    # Offline price timestamp check (valid up to 3 days in the past)
    now = datetime.now(tz=UTC)
    is_offline = bool(data.get("offline", False))
    recorded_at: datetime | None = None
    price_at: datetime | None = None
    offline_days = int(rules.get("offline_max_days", 3))
    if data.get("recorded_at"):
        try:
            recorded_at = datetime.fromisoformat(str(data["recorded_at"]).replace("Z", "+00:00"))
            if recorded_at.tzinfo is None:
                recorded_at = recorded_at.replace(tzinfo=UTC)
        except ValueError as exc:
            raise ApiException(code=ErrorCode.VALIDATION, message=("تاريخ تسجيل الطلب غير صالح", "Invalid recorded_at timestamp")) from exc
        if (now - timedelta(days=offline_days)) <= recorded_at <= now:
            price_at = recorded_at

    total_qty_input = 0
    requested_specs: list[dict[str, Any]] = []
    allowed_cats = set(actor.rep_categories or []) if actor.role == Role.REP else set()

    req_item_ids: set[int] = set()
    for ln in raw_lines:
        try:
            req_item_ids.add(int(ln["item_id"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise ApiException(code=ErrorCode.VALIDATION, message=("بيانات المادة أو الكمية غير صالحة", "Invalid order line")) from exc

    # Lock target Item rows in deterministic ID order so concurrent order/transfer stock checks serialize cleanly
    items_map = {
        it.id: it
        for it in Item.objects.select_for_update().filter(id__in=sorted(req_item_ids), active=True).order_by("id")
    }

    for ln in raw_lines:
        try:
            iid = int(ln["item_id"])
            item = items_map[iid]
            unit = ln["unit"]
            if unit not in ("carton", "piece"):
                raise ValueError
            qty = int(ln["qty"])
            if qty <= 0:
                raise ValueError
        except (KeyError, TypeError, ValueError) as exc:
            raise ApiException(code=ErrorCode.VALIDATION, message=("بيانات المادة أو الكمية غير صالحة", "Invalid order line")) from exc

        if allowed_cats and item.category not in allowed_cats:
            raise ApiException(
                code=ErrorCode.FORBIDDEN,
                message=(f"غير مصرح لك ببيع صنف {item.category}", f"Rep is not authorized to sell category {item.category}"),
                status_code=http_status.HTTP_403_FORBIDDEN,
            )

        if is_count_locked(wh.id, item.category):
            raise ApiException(
                code=ErrorCode.BAD_STATE,
                message=("المخزن قيد الجرد حالياً لهذه الفئة", "Warehouse is locked by an open stocktake"),
            )

        total_qty_input += qty
        pieces = qty * item.pieces_per_carton if unit == "carton" else qty
        requested_specs.append({"item": item, "unit": unit, "qty": qty, "pieces": pieces})

    if actor.role == Role.SHOP and total_qty_input > SHOP_MAX_QTY:
        raise ApiException(
            code=ErrorCode.VALIDATION,
            message=(f"الحد الأقصى لطلب المحل {SHOP_MAX_QTY} وحدة", f"Shop orders cannot exceed {SHOP_MAX_QTY} units"),
        )

    # Stock check / offline partial split
    avail_tracker: dict[int, int] = {}
    active_specs: list[dict[str, Any]] = []
    waiting_specs: list[dict[str, Any]] = []

    for spec in requested_specs:
        it: Item = spec["item"]
        if it.id not in avail_tracker:
            avail_tracker[it.id] = shelf_available(wh.id, it.id)
        avail = avail_tracker[it.id]
        need = spec["pieces"]

        if not is_offline:
            if avail < need:
                raise ApiException(
                    code=ErrorCode.OUT_OF_STOCK,
                    message=(f"الكمية المتوفرة غير كافية للمادة {it.name}", f"Insufficient stock for {it.name_en}"),
                    status_code=http_status.HTTP_409_CONFLICT,
                )
            take = need
            short = 0
        else:
            take = min(avail, need)
            short = need - take

        avail_tracker[it.id] -= take

        if take > 0:
            if spec["unit"] == "carton" and take % it.pieces_per_carton == 0:
                u_out, q_out = "carton", take // it.pieces_per_carton
            elif spec["unit"] == "carton":
                u_out, q_out = "piece", take
            else:
                u_out, q_out = "piece", take
            up = _resolve_unit_price(it, customer, u_out, price_at)
            active_specs.append({"item": it, "unit": u_out, "qty": q_out, "pieces": take, "unit_price": up, "total": q_out * up})

        if short > 0:
            if spec["unit"] == "carton" and short % it.pieces_per_carton == 0:
                u_w, q_w = "carton", short // it.pieces_per_carton
            else:
                u_w, q_w = "piece", short
            up_w = _resolve_unit_price(it, customer, u_w, price_at)
            waiting_specs.append({"item": it, "unit": u_w, "qty": q_w, "pieces": short, "unit_price": up_w, "total": q_w * up_w})

    discount_pct = _parse_discount_percent(data.get("discount_percent"), actor.role, rules)
    if discount_pct > 0:
        may_profile_action(actor, "discount")
    settings_obj = SystemSetting.get_solo()
    source = OrderSource.SHOP if actor.role == Role.SHOP else (OrderSource.REP if actor.role == Role.REP else OrderSource.OFFICE)
    rep_user_id = actor.id if actor.role == Role.REP else customer.rep_id
    notes = str(data.get("note") if "note" in data else data.get("notes") or "").strip()

    def _persist_order(
        *,
        specs: list[dict[str, Any]],
        status_val: str,
        waiting_flag: bool,
        approval_reasons: list[str],
        cref: str | None,
        parent_order: Order | None = None,
    ) -> Order:
        eff_disc = discount_pct if not waiting_flag else Decimal("0")
        sub, disc_amt, rnd, tot = _compute_totals(specs, eff_disc)
        res_until = (
            now + timedelta(hours=settings_obj.reservation_hours)
            if status_val in (OrderStatus.PENDING, OrderStatus.NEEDS_APPROVAL)
            else None
        )
        is_auto_rep = (
            status_val == OrderStatus.APPROVED
            and source == OrderSource.REP
            and bool(rules.get("auto_confirm_rep_orders", False))
        )
        o = Order.objects.create(
            number=f"TMP-{uuid4().hex[:10]}",
            customer=customer,
            warehouse=wh,
            rep_id=rep_user_id,
            created_by=actor,
            source=source,
            status=status_val,
            waiting_stock=waiting_flag,
            approval_for=approval_reasons,
            notes=notes,
            subtotal=sub,
            discount_percent=eff_disc,
            discount_amount=disc_amt,
            discount_by=actor if eff_disc > 0 else None,
            discount_at=now if eff_disc > 0 else None,
            accepted_by=None if is_auto_rep else (actor if status_val == OrderStatus.APPROVED else None),
            accepted_at=now if status_val == OrderStatus.APPROVED else None,
            rounding=rnd,
            total=tot,
            reserved_until=res_until,
            client_ref=cref,
            offline=is_offline,
            recorded_at=recorded_at,
            parent=parent_order,
        )
        o.number = f"ORD-{o.id:04d}"
        o.save(update_fields=["number"])
        for sp in specs:
            OrderLine.objects.create(
                order=o,
                item=sp["item"],
                warehouse=wh,
                unit=sp["unit"],
                qty=sp["qty"],
                pieces=sp["pieces"],
                unit_price=sp["unit_price"],
                total=sp["total"],
            )
        return o

    # Case 1: completely out of stock on offline order -> single waiting_stock order
    if not active_specs and waiting_specs:
        waiting_order = _persist_order(
            specs=waiting_specs,
            status_val=OrderStatus.EXPIRED,
            waiting_flag=True,
            approval_reasons=[],
            cref=client_ref,
        )
        validate_and_apply_custom_on_create("order", waiting_order.id, actor, data.get("custom"))
        notify_role(
            role=Role.MANAGER,
            kind="order_waiting_stock",
            title_ar=f"طلب بانتظار البضاعة {waiting_order.number}",
            title_en=f"Offline order waiting for stock {waiting_order.number}",
            ref_type="order",
            ref_id=waiting_order.id,
        )
        record_audit(actor=actor, action="order.create_waiting", entity_type="order", entity_id=waiting_order.id)
        publish_order_changed(waiting_order.id)
        return get_order_for_actor(actor=actor, order_id=waiting_order.id)

    _, _, _, active_total = _compute_totals(active_specs, discount_pct)
    min_shop_order = int(rules.get("min_shop_order") or 0)
    if actor.role == Role.SHOP and min_shop_order > 0 and active_total < min_shop_order:
        raise ApiException(
            code=ErrorCode.MIN_ORDER,
            message=(
                f"أقل طلب {min_shop_order:,} د.ع",
                f"The smallest order is {min_shop_order:,} IQD",
            ),
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            errors={
                "lines": (
                    f"أضف ما يصل بالطلب إلى {min_shop_order:,} د.ع",
                    f"Add up to {min_shop_order:,} IQD",
                )
            },
        )

    bal = customer_balance_map([customer.id]).get(customer.id, 0)
    committed = _committed_order_debt(customer.id)
    over_limit = (bal + committed + active_total) > customer.debt_limit
    over_limit_mode = str(rules.get("over_limit") or "approval")
    if over_limit and actor.role != Role.MANAGER and over_limit_mode == "block":
        raise ApiException(
            code=ErrorCode.DEBT_LIMIT,
            message=("الطلب يتجاوز سقف دين الزبون", "The order passes the customer's debt limit"),
            status_code=http_status.HTTP_409_CONFLICT,
        )

    staff_disc_max = Decimal(str(int(rules.get("staff_discount_max", int(STAFF_DISCOUNT_MAX)))))
    approval_for: list[str] = []
    if actor.role != Role.MANAGER:
        if over_limit and over_limit_mode == "approval":
            approval_for.append("debt")
        if discount_pct > staff_disc_max:
            approval_for.append("discount")

    if approval_for:
        initial_status = OrderStatus.NEEDS_APPROVAL
    else:
        initial_status = OrderStatus.APPROVED

    primary_order = _persist_order(
        specs=active_specs,
        status_val=initial_status,
        waiting_flag=False,
        approval_reasons=approval_for,
        cref=client_ref,
    )
    validate_and_apply_custom_on_create("order", primary_order.id, actor, data.get("custom"))

    rule_approvals = run_automation_rules(
        "order_created",
        {
            "total": active_total,
            "discount": float(discount_pct),
            "lines": len(active_specs),
            "by_role": actor.role,
            "area": customer.area,
        },
        number=primary_order.number,
        link=f"/orders/{primary_order.id}",
        by=actor.name,
        customer=customer.name,
    )
    if rule_approvals and actor.role != Role.MANAGER:
        if "rule" not in approval_for:
            approval_for.append("rule")
        primary_order.approval_for = approval_for
        primary_order.rule_note = " · ".join(rule_approvals)
        primary_order.status = OrderStatus.NEEDS_APPROVAL
        if primary_order.reserved_until is None:
            primary_order.reserved_until = now + timedelta(hours=settings_obj.reservation_hours)
        primary_order.accepted_by = None
        primary_order.accepted_at = None
        primary_order.save(
            update_fields=[
                "approval_for",
                "rule_note",
                "status",
                "reserved_until",
                "accepted_by",
                "accepted_at",
            ]
        )
        initial_status = OrderStatus.NEEDS_APPROVAL

    wait_child: Order | None = None
    if waiting_specs:
        rest_cref = f"{client_ref}-rest" if client_ref else None
        wait_child = _persist_order(
            specs=waiting_specs,
            status_val=OrderStatus.EXPIRED,
            waiting_flag=True,
            approval_reasons=[],
            cref=rest_cref,
            parent_order=primary_order,
        )
        primary_order.rest_order = wait_child
        primary_order.save(update_fields=["rest_order"])

    if initial_status == OrderStatus.NEEDS_APPROVAL:
        notify_role(
            role=Role.MANAGER,
            kind="order_needs_approval",
            title_ar=f"طلب يحتاج موافقة {primary_order.number}",
            title_en=f"Order requires approval {primary_order.number}",
            ref_type="order",
            ref_id=primary_order.id,
        )
    else:
        if over_limit and actor.role != Role.MANAGER and over_limit_mode == "allow":
            notify_role(
                role=Role.MANAGER,
                kind="needs_approval",
                title_ar=f"طلب تجاوز سقف الدين (سُمح به): {customer.name}",
                title_en=f"Over-limit order allowed: {customer.name}",
                body_ar=f"{primary_order.number} · {active_total:,} د.ع",
                body_en=f"{primary_order.number} · {active_total:,} IQD",
                ref_type="order",
                ref_id=primary_order.id,
            )
        if initial_status == OrderStatus.PENDING:
            notify_role(
                role=Role.ACCOUNTANT,
                kind="order_review",
                title_ar=f"طلب بانتظار الاعتماد: {customer.name}",
                title_en=f"Order to approve: {customer.name}",
                ref_type="order",
                ref_id=primary_order.id,
            )
            notify_role(
                role=Role.WAREHOUSE,
                kind="order_pending",
                title_ar=f"طلب جديد {primary_order.number}",
                title_en=f"New order {primary_order.number}",
                ref_type="order",
                ref_id=primary_order.id,
            )
        elif initial_status == OrderStatus.APPROVED:
            notify_role(
                role=Role.WAREHOUSE,
                kind="order_accepted",
                title_ar=f"طلب جاهز للتجهيز {primary_order.number}",
                title_en=f"Order ready to prepare {primary_order.number}",
                ref_type="order",
                ref_id=primary_order.id,
            )

    record_audit(
        actor=actor,
        action="order.create",
        entity_type="order",
        entity_id=primary_order.id,
        summary=f"Created order {primary_order.number} ({initial_status})",
        after={"status": initial_status, "total": primary_order.total},
    )
    publish_order_changed(primary_order.id)
    if wait_child is not None:
        publish_order_changed(wait_child.id)
    if actor.role == Role.REP:
        from apps.customers.visits_services import record_auto_visit

        record_auto_visit(actor=actor, customer=customer, outcome="order", order_id=primary_order.id)
    return get_order_for_actor(actor=actor, order_id=primary_order.id)


@transaction.atomic
def accept_order(*, actor: User, order_id: int) -> dict[str, Any]:
    order = Order.objects.select_for_update().prefetch_related("lines__item").filter(id=order_id).first()
    if not order:
        raise ApiException(code=ErrorCode.NOT_FOUND, status_code=http_status.HTTP_404_NOT_FOUND)
    if order.status != OrderStatus.PENDING:
        raise ApiException(code=ErrorCode.BAD_STATE, message=("الطلب ليس بانتظار الاعتماد", "Order is not pending acceptance"))

    line_item_ids = sorted({ln.item_id for ln in order.lines.all()})
    if line_item_ids:
        list(Item.objects.select_for_update().filter(id__in=line_item_ids).order_by("id"))

    for ln in order.lines.all():
        if shelf_available(order.warehouse_id, ln.item_id, exclude_order_id=order.id) < ln.pieces:
            raise ApiException(code=ErrorCode.OUT_OF_STOCK, status_code=http_status.HTTP_409_CONFLICT)

    now = datetime.now(tz=UTC)
    order.status = OrderStatus.APPROVED
    order.reserved_until = None
    order.accepted_by = actor
    order.accepted_at = now
    order.save(update_fields=["status", "reserved_until", "accepted_by", "accepted_at"])

    notify_role(
        role=Role.WAREHOUSE,
        kind="order_accepted",
        title_ar=f"طلب معتمد جاهز للتجهيز {order.number}",
        title_en=f"Approved order ready to prepare {order.number}",
        ref_type="order",
        ref_id=order.id,
    )

    if order.source == OrderSource.SHOP:
        for ms in order.customer.memberships.all():
            notify_user(
                user_id=ms.user_id,
                kind="order_accepted",
                title_ar=f"تم قبول طلبك {order.number}",
                title_en=f"Your order {order.number} was accepted",
                ref_type="order",
                ref_id=order.id,
            )

    record_audit(actor=actor, action="order.accept", entity_type="order", entity_id=order.id)
    publish_order_changed(order.id)
    return get_order_for_actor(actor=actor, order_id=order.id)


@transaction.atomic
def approve_order(*, actor: User, order_id: int) -> dict[str, Any]:
    order = Order.objects.select_for_update().prefetch_related("lines").filter(id=order_id).first()
    if not order:
        raise ApiException(code=ErrorCode.NOT_FOUND, status_code=http_status.HTTP_404_NOT_FOUND)
    if order.status != OrderStatus.NEEDS_APPROVAL:
        raise ApiException(code=ErrorCode.BAD_STATE, message=("الطلب لا يحتاج موافقة", "Order does not require approval"))

    reasons = list(order.approval_for or [])
    if ("discount" in reasons or "rule" in reasons) and actor.role != Role.MANAGER:
        raise ApiException(
            code=ErrorCode.FORBIDDEN,
            message=("الموافقة على الخصم العالي تتطلب صلاحية المدير", "Only manager can approve high-discount orders"),
            status_code=http_status.HTTP_403_FORBIDDEN,
        )

    line_item_ids = sorted({ln.item_id for ln in order.lines.all()})
    if line_item_ids:
        list(Item.objects.select_for_update().filter(id__in=line_item_ids).order_by("id"))

    for ln in order.lines.all():
        if shelf_available(ln.warehouse_id or order.warehouse_id, ln.item_id, exclude_order_id=order.id) < ln.pieces:
            raise ApiException(code=ErrorCode.OUT_OF_STOCK, status_code=http_status.HTTP_409_CONFLICT)

    now = datetime.now(tz=UTC)
    order.status = OrderStatus.APPROVED
    order.approval_for = []
    order.reserved_until = None
    order.approved_by = actor
    order.approved_at = now
    order.accepted_by = actor
    order.accepted_at = now
    order.save(update_fields=["status", "approval_for", "reserved_until", "approved_by", "approved_at", "accepted_by", "accepted_at"])

    record_audit(actor=actor, action="order.approve", entity_type="order", entity_id=order.id, summary=f"Approved {order.number}")
    publish_order_changed(order.id)
    return get_order_for_actor(actor=actor, order_id=order.id)


def get_order_prep_plan(*, actor: User, order_id: int) -> list[dict[str, Any]]:
    from apps.common.utils import baghdad_today
    from apps.inventory.models import Batch

    order = Order.objects.prefetch_related("lines").filter(id=order_id).first()
    if not order:
        raise ApiException(code=ErrorCode.NOT_FOUND, status_code=http_status.HTTP_404_NOT_FOUND)
    if not can_see_order(actor, order):
        raise ApiException(code=ErrorCode.FORBIDDEN, status_code=http_status.HTTP_403_FORBIDDEN)

    today = baghdad_today()
    warehouses = list(Warehouse.objects.filter(active=True).order_by("id"))
    item_ids = list({ln.item_id for ln in order.lines.all()})
    exp_rows = (
        Batch.objects.filter(item_id__in=item_ids, qty_left__gt=0, expiry_date__gte=today, held_at__isnull=True)
        .values("warehouse_id", "item_id")
        .annotate(nearest=Min("expiry_date"))
    )
    nearest_map = {
        (r["warehouse_id"], r["item_id"]): r["nearest"].isoformat()
        for r in exp_rows
        if r["nearest"]
    }

    plan_lines: list[dict[str, Any]] = []
    for ln in order.lines.order_by("id"):
        free = []
        candidates: list[tuple[str, int]] = []
        for w in warehouses:
            avail = shelf_available(w.id, ln.item_id, exclude_order_id=order.id)
            near = nearest_map.get((w.id, ln.item_id))
            entry: dict[str, Any] = {"warehouse_id": w.id, "pieces": avail}
            if near:
                entry["nearest_expiry"] = near
            free.append(entry)
            if avail >= ln.pieces and near:
                candidates.append((near, w.id))
        suggested = min(candidates)[1] if candidates else None
        plan_lines.append(
            {
                "line_id": ln.id,
                "item_id": ln.item_id,
                "unit": ln.unit,
                "qty": ln.qty,
                "suggested_warehouse_id": suggested,
                "free": free,
            }
        )
    return plan_lines


def _resolve_carrier_pick(
    *,
    rep_id: Any = None,
    vehicle_id: Any = None,
    require_rep: bool = False,
) -> tuple[User | None, Vehicle | None]:
    """Validate optional rep_id and vehicle_id; vehicle brings its own driver when rep_id omitted."""
    vehicle: Vehicle | None = None
    if vehicle_id not in (None, "", 0, "0"):
        try:
            vid = int(vehicle_id)
        except (TypeError, ValueError) as exc:
            raise ApiException(
                code=ErrorCode.VALIDATION,
                message=("سيارة غير موجودة أو موقوفة", "Unknown or stopped vehicle"),
                status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
                errors={"vehicle_id": ("اختر سيارة أخرى", "Pick another vehicle")},
            ) from exc
        vehicle = Vehicle.objects.select_related("driver").filter(id=vid, active=True).first()
        if vehicle is None:
            raise ApiException(
                code=ErrorCode.VALIDATION,
                message=("سيارة غير موجودة أو موقوفة", "Unknown or stopped vehicle"),
                status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
                errors={"vehicle_id": ("اختر سيارة أخرى", "Pick another vehicle")},
            )

    if require_rep and rep_id in (None, "", 0, "0"):
        raise ApiException(
            code=ErrorCode.VALIDATION,
            message=("اختر مندوب التوصيل", "Pick the delivery rep"),
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            errors={"rep_id": ("مطلوب", "Required")},
        )

    rep: User | None = None
    if rep_id not in (None, "", 0, "0"):
        try:
            rid = int(rep_id)
        except (TypeError, ValueError):
            rid = -1
        candidate = User.objects.filter(id=rid, role=Role.REP, active=True, deleted=False).first()
        if candidate is None or not candidate.has_rep_kind("delivery"):
            raise ApiException(
                code=ErrorCode.VALIDATION,
                message=("اختر مندوب توصيل", "Pick a delivery rep"),
                status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
                errors={"rep_id": ("هذا ليس مندوب توصيل فعّالاً", "Not an active delivery rep")},
            )
        rep = candidate
    elif vehicle is not None and vehicle.driver is not None:
        cand = vehicle.driver
        if cand.role == Role.REP and cand.active and not cand.deleted and cand.has_rep_kind("delivery"):
            rep = cand

    return rep, vehicle


@transaction.atomic
def prepare_order(
    *,
    actor: User,
    order_id: int,
    warehouses_map: dict[str, Any],
    rep_id: Any = None,
    vehicle_id: Any = None,
) -> dict[str, Any]:
    order = (
        Order.objects.select_for_update()
        .select_related("customer")
        .prefetch_related("lines__item", "prep_parts")
        .filter(id=order_id)
        .first()
    )
    if not order:
        raise ApiException(code=ErrorCode.NOT_FOUND, status_code=http_status.HTTP_404_NOT_FOUND)
    if order.status != OrderStatus.APPROVED:
        raise ApiException(code=ErrorCode.BAD_STATE, message=("يجب اعتماد الطلب قبل تجهيزه", "Order must be approved before preparation"))
    if hasattr(order, "delivery"):
        raise ApiException(code=ErrorCode.BAD_STATE, message=("تم تجهيز هذا الطلب مسبقاً", "Order delivery has already been prepared"))

    lines = list(order.lines.order_by("id"))
    existing_parts = {p.warehouse_id: p for p in order.prep_parts.all()}

    if not existing_parts:
        # First preparation pass assigns per-line warehouses and validates stock + locks
        if actor.role in (Role.WAREHOUSE, Role.STAFF) and not works_in_warehouse(actor, order.warehouse_id):
            raise ApiException(code=ErrorCode.FORBIDDEN, status_code=http_status.HTTP_403_FORBIDDEN)
        wmap = {str(k): v for k, v in (warehouses_map or {}).items()}
        key_set = set(wmap.keys())
        item_id_keys = {str(ln.item_id) for ln in lines}
        line_id_keys = {str(ln.id) for ln in lines}
        use_item_key = bool(key_set) and key_set.issubset(item_id_keys) and (
            not key_set.issubset(line_id_keys) or item_id_keys == key_set
        )
        valid_whs = {w.id for w in Warehouse.objects.filter(active=True)}
        per_wh_item_need: dict[tuple[int, int], int] = {}
        assigned_whs: set[int] = set()
        for ln in lines:
            if use_item_key:
                chosen = wmap.get(str(ln.item_id), wmap.get(str(ln.id), ln.warehouse_id or order.warehouse_id))
            else:
                chosen = wmap.get(str(ln.id), wmap.get(str(ln.item_id), ln.warehouse_id or order.warehouse_id))
            try:
                chosen_id = int(chosen)
            except (TypeError, ValueError) as exc:
                raise ApiException(code=ErrorCode.VALIDATION, message=("مخزن التجهيز غير صالح", "Invalid warehouse selection")) from exc
            if chosen_id not in valid_whs:
                raise ApiException(code=ErrorCode.VALIDATION, message=("المخزن غير موجود", "Warehouse not found"))
            if is_count_locked(chosen_id, ln.item.category):
                raise ApiException(code=ErrorCode.BAD_STATE, message=("المخزن قيد الجرد لهذه الفئة", "Warehouse locked by stocktake"))
            ln.warehouse_id = chosen_id
            ln.save(update_fields=["warehouse"])
            per_wh_item_need[(chosen_id, ln.item_id)] = per_wh_item_need.get((chosen_id, ln.item_id), 0) + ln.pieces
            assigned_whs.add(chosen_id)

        prep_item_ids = sorted({it_i for (_, it_i) in per_wh_item_need})
        if prep_item_ids:
            list(Item.objects.select_for_update().filter(id__in=prep_item_ids).order_by("id"))

        for (wh_i, it_i), need_pcs in per_wh_item_need.items():
            if shelf_available(wh_i, it_i, exclude_order_id=order.id) < need_pcs:
                raise ApiException(
                    code=ErrorCode.OUT_OF_STOCK,
                    message=("المخزون غير كافٍ في المخزن المحدد", "Insufficient stock in selected preparation warehouse"),
                    status_code=http_status.HTTP_409_CONFLICT,
                )

        for wh_i in sorted(assigned_whs):
            existing_parts[wh_i] = PrepPart.objects.create(order=order, warehouse_id=wh_i)

    has_incoming_pick = rep_id not in (None, "", 0, "0") or vehicle_id not in (None, "", 0, "0")
    incoming_pick: dict[str, int] | None = None
    if has_incoming_pick:
        picked_rep, picked_vehicle = _resolve_carrier_pick(rep_id=rep_id, vehicle_id=vehicle_id)
        incoming_pick = {}
        if rep_id not in (None, "", 0, "0") and picked_rep is not None:
            incoming_pick["rep_id"] = picked_rep.id
        if picked_vehicle is not None:
            incoming_pick["vehicle_id"] = picked_vehicle.id

    now = datetime.now(tz=UTC)
    if actor.role in (Role.WAREHOUSE, Role.STAFF):
        allowed_parts = [p for wid, p in existing_parts.items() if works_in_warehouse(actor, wid)]
        if not allowed_parts:
            raise ApiException(code=ErrorCode.FORBIDDEN, status_code=http_status.HTTP_403_FORBIDDEN)
        for part in allowed_parts:
            if part.prepared_by_id is None:
                part.prepared_by = actor
                part.prepared_at = now
                part.save(update_fields=["prepared_by", "prepared_at"])
    else:
        # Manager completes preparation across parts
        for part in existing_parts.values():
            if part.prepared_by_id is None:
                part.prepared_by = actor
                part.prepared_at = now
                part.save(update_fields=["prepared_by", "prepared_at"])

    unprepared = [p for p in existing_parts.values() if p.prepared_by_id is None]
    if unprepared:
        if incoming_pick is not None:
            order.carrier_pick = incoming_pick
            order.save(update_fields=["carrier_pick", "updated_at"])
        notify_role(
            role=Role.WAREHOUSE,
            kind="order_partial_prep",
            title_ar=f"الطلب {order.number} ينتظر استكمال التجهيز",
            title_en=f"Order {order.number} awaiting preparation in another warehouse",
            ref_type="order",
            ref_id=order.id,
        )
    else:
        effective_pick = incoming_pick if incoming_pick is not None else order.carrier_pick
        if order.carrier_pick is not None:
            order.carrier_pick = None
            order.save(update_fields=["carrier_pick", "updated_at"])

        chosen_rep: User | None = None
        chosen_vehicle: Vehicle | None = None
        if effective_pick:
            chosen_rep, chosen_vehicle = _resolve_carrier_pick(
                rep_id=effective_pick.get("rep_id"),
                vehicle_id=effective_pick.get("vehicle_id"),
            )

        cust_rep = order.customer.rep
        fallback_rep = cust_rep if (cust_rep and cust_rep.active and cust_rep.has_rep_kind("delivery")) else None
        del_rep = chosen_rep or fallback_rep
        vehicle = (
            chosen_vehicle
            if chosen_vehicle is not None
            else (Vehicle.objects.filter(driver=del_rep, active=True).order_by("id").first() if del_rep else None)
        )
        pin = _generate_delivery_pin()
        Delivery.objects.create(
            order=order,
            code=_generate_delivery_code(),
            pin_encrypted=encrypt_secret(pin),
            pin_hash=make_password(pin),
            rep=del_rep,
            vehicle=vehicle,
            status=DeliveryStatus.READY,
        )
        if vehicle is not None:
            from apps.deliveries.services import lines_weight_kg

            kg = lines_weight_kg(lines)
            if kg > vehicle.capacity_kg:
                notify_role(
                    role=Role.WAREHOUSE,
                    kind="delivery_ready",
                    title_ar=f"تنبيه حمولة {order.number}",
                    title_en=f"Weight alert for {order.number}",
                    body_ar=f"وزن الطلب ({int(round(kg))} كغم) يتجاوز حمولة {vehicle.name} ({vehicle.capacity_kg} كغم).",
                    body_en=f"Order weight ({int(round(kg))} kg) exceeds {vehicle.name} capacity ({vehicle.capacity_kg} kg).",
                    ref_type="order",
                    ref_id=order.id,
                )
        if effective_pick and del_rep is not None:
            notify_user(
                user_id=del_rep.id,
                kind="order_accepted",
                title_ar=f"شحنة لك: {order.number}",
                title_en=f"A shipment for you: {order.number}",
                body_ar=f"على {vehicle.name}، جاهزة في المخزن" if vehicle else "جاهزة في المخزن",
                body_en="Ready at the warehouse",
                ref_type="order",
                ref_id=order.id,
            )
        else:
            notify_role(
                role=Role.REP,
                kind="delivery_ready",
                title_ar=f"طلب جاهز للتوصيل {order.number}",
                title_en=f"Order ready for delivery {order.number}",
                ref_type="order",
                ref_id=order.id,
            )

    record_audit(
        actor=actor,
        action="order.prepare",
        entity_type="order",
        entity_id=order.id,
        summary=(
            f"Prepared {order.number}"
            + (f" (driver={del_rep.name})" if not unprepared and effective_pick and del_rep else "")
        ),
    )
    publish_order_changed(order.id)
    return get_order_for_actor(actor=actor, order_id=order.id)


@transaction.atomic
def set_order_carrier(*, actor: User, order_id: int, data: dict[str, Any]) -> dict[str, Any]:
    """Update delivery rep and optional vehicle (or null for no vehicle) before a shipment is loaded."""
    if actor.role not in (Role.MANAGER, Role.WAREHOUSE, Role.STAFF):
        raise ApiException(code=ErrorCode.FORBIDDEN, status_code=http_status.HTTP_403_FORBIDDEN)

    order = (
        Order.objects.select_for_update()
        .select_related("customer", "delivery", "delivery__rep", "delivery__vehicle")
        .prefetch_related("prep_parts", "shipment_children")
        .filter(id=order_id)
        .first()
    )
    if not order:
        raise ApiException(code=ErrorCode.NOT_FOUND, status_code=http_status.HTTP_404_NOT_FOUND)

    if actor.role in (Role.WAREHOUSE, Role.STAFF) and not works_in_warehouse(actor, order.warehouse_id):
        if not any(works_in_warehouse(actor, p.warehouse_id) for p in order.prep_parts.all()):
            raise ApiException(
                code=ErrorCode.FORBIDDEN,
                message=("هذا الطلب من مخزن آخر", "This order is from another warehouse"),
                status_code=http_status.HTTP_403_FORBIDDEN,
            )

    dlv = getattr(order, "delivery", None)
    if not dlv or dlv.status != DeliveryStatus.READY or order.shipment_children.exists():
        raise ApiException(
            code=ErrorCode.BAD_STATE,
            message=("يُغيَّر السائق بعد التجهيز وقبل خروج الشحنة", "The driver changes after preparing and before it leaves"),
            status_code=http_status.HTTP_409_CONFLICT,
        )
    if dlv.loaded_at is not None:
        raise ApiException(
            code=ErrorCode.BAD_STATE,
            message=("حُمّلت الشحنة: لا يمكن تغيير سائقها الآن", "The shipment is loaded: its driver can't change now"),
            status_code=http_status.HTTP_409_CONFLICT,
        )

    rep, vehicle = _resolve_carrier_pick(
        rep_id=data.get("rep_id"),
        vehicle_id=data.get("vehicle_id"),
        require_rep=True,
    )
    assert rep is not None

    before_rep_id = dlv.rep_id
    dlv.rep = rep
    dlv.vehicle = vehicle
    dlv.save(update_fields=["rep", "vehicle"])

    record_audit(
        actor=actor,
        action="update",
        entity_type="order",
        entity_id=order.id,
        summary=f"{order.number}: {rep.name} delivers it" + (f" on {vehicle.name}" if vehicle else ""),
        summary_ar=f"{order.number}: يوصله {rep.name}{f' على {vehicle.name}' if vehicle else ' بلا سيارة محددة'}",
    )
    if before_rep_id != rep.id:
        notify_user(
            user_id=rep.id,
            kind="order_accepted",
            title_ar=f"شحنة لك: {order.number}",
            title_en=f"A shipment for you: {order.number}",
            body_ar=f"على {vehicle.name}، جاهزة في المخزن" if vehicle else "جاهزة في المخزن",
            body_en="Ready at the warehouse",
            ref_type="order",
            ref_id=order.id,
        )

    publish_order_changed(order.id)
    return get_order_for_actor(actor=actor, order_id=order.id)


@transaction.atomic
def update_order(*, actor: User, order_id: int, data: dict[str, Any]) -> dict[str, Any]:
    order = Order.objects.select_for_update().select_related("customer").filter(id=order_id).first()
    if not order:
        raise ApiException(code=ErrorCode.NOT_FOUND, status_code=http_status.HTTP_404_NOT_FOUND)
    if not can_see_order(actor, order):
        raise ApiException(code=ErrorCode.FORBIDDEN, status_code=http_status.HTTP_403_FORBIDDEN)
    if order.status not in (OrderStatus.PENDING, OrderStatus.NEEDS_APPROVAL, OrderStatus.APPROVED) or hasattr(order, "delivery"):
        raise ApiException(code=ErrorCode.BAD_STATE, message=("لا يمكن تعديل الطلب بعد التجهيز", "Order cannot be edited after preparation"))

    if actor.role == Role.SHOP and (
        order.status not in (OrderStatus.PENDING, OrderStatus.NEEDS_APPROVAL, OrderStatus.APPROVED)
        or order.prep_parts.exists()
    ):
        raise ApiException(code=ErrorCode.FORBIDDEN, status_code=http_status.HTTP_403_FORBIDDEN)

    rules = rules_of()
    if actor.role == Role.SHOP:
        edit_mins = int(rules.get("shop_edit_minutes") or 0)
        if edit_mins > 0 and (datetime.now(tz=UTC) - order.created_at).total_seconds() > edit_mins * 60:
            raise ApiException(
                code=ErrorCode.EDIT_CLOSED,
                message=(f"انتهى وقت التعديل ({edit_mins} د من الطلب)", f"Edit window closed ({edit_mins} min)"),
                status_code=http_status.HTTP_409_CONFLICT,
            )

    if "note" in data or "notes" in data:
        raw_note = data.get("note") if "note" in data else data.get("notes")
        order.notes = str(raw_note or "").strip()

    if "warehouse_id" in data and data["warehouse_id"]:
        wh = Warehouse.objects.filter(id=data["warehouse_id"], active=True).first()
        if not wh:
            raise ApiException(code=ErrorCode.VALIDATION, message=("المخزن غير موجود", "Warehouse not found"))
        order.warehouse = wh

    if "lines" in data and isinstance(data["lines"], list) and data["lines"]:
        upd_ids: set[int] = set()
        for ln in data["lines"]:
            try:
                upd_ids.add(int(ln["item_id"]))
            except (KeyError, TypeError, ValueError) as exc:
                raise ApiException(code=ErrorCode.VALIDATION, message=("سطور الطلب غير صالحة", "Invalid order lines")) from exc
        items_map = {
            it.id: it
            for it in Item.objects.select_for_update().filter(id__in=sorted(upd_ids), active=True).order_by("id")
        }
        avail_tracker: dict[int, int] = {}
        specs = []
        for ln in data["lines"]:
            try:
                it = items_map[int(ln["item_id"])]
                unit = ln["unit"]
                qty = int(ln["qty"])
                if unit not in ("carton", "piece") or qty <= 0:
                    raise ValueError
            except (KeyError, TypeError, ValueError) as exc:
                raise ApiException(code=ErrorCode.VALIDATION, message=("سطور الطلب غير صالحة", "Invalid order lines")) from exc
            pcs = qty * it.pieces_per_carton if unit == "carton" else qty
            if it.id not in avail_tracker:
                avail_tracker[it.id] = shelf_available(order.warehouse_id, it.id, exclude_order_id=order.id)
            if avail_tracker[it.id] < pcs:
                raise ApiException(code=ErrorCode.OUT_OF_STOCK, status_code=http_status.HTTP_409_CONFLICT)
            avail_tracker[it.id] -= pcs
            up = _resolve_unit_price(it, order.customer, unit, None)
            specs.append({"item": it, "unit": unit, "qty": qty, "pieces": pcs, "unit_price": up, "total": qty * up})

        order.lines.all().delete()
        for sp in specs:
            OrderLine.objects.create(
                order=order,
                item=sp["item"],
                warehouse_id=order.warehouse_id,
                unit=sp["unit"],
                qty=sp["qty"],
                pieces=sp["pieces"],
                unit_price=sp["unit_price"],
                total=sp["total"],
            )
    else:
        specs = [{"total": ln.total} for ln in order.lines.all()]

    disc = _parse_discount_percent(data.get("discount_percent", order.discount_percent), actor.role, rules)
    sub, disc_amt, rnd, tot = _compute_totals(specs, disc)
    order.discount_percent = disc
    order.subtotal = sub
    order.discount_amount = disc_amt
    order.rounding = rnd
    order.total = tot
    order.edited_at = datetime.now(tz=UTC)

    # Re-evaluate debt limit when non-manager edits order lines or totals.
    if actor.role != Role.MANAGER:
        bal = customer_balance_map([order.customer_id]).get(order.customer_id, 0)
        committed = _committed_order_debt(order.customer_id, exclude_order_id=order.id)
        over_limit = (bal + committed + tot) > order.customer.debt_limit
        over_limit_mode = str(rules.get("over_limit") or "approval")
        if over_limit and over_limit_mode == "block":
            raise ApiException(
                code=ErrorCode.DEBT_LIMIT,
                message=("الطلب يتجاوز سقف دين الزبون", "The order passes the customer's debt limit"),
                status_code=http_status.HTTP_409_CONFLICT,
            )
        reasons = [r for r in (order.approval_for or []) if r != "debt"]
        if over_limit and over_limit_mode == "approval":
            reasons.append("debt")
        staff_disc_max = Decimal(str(int(rules.get("staff_discount_max", int(STAFF_DISCOUNT_MAX)))))
        if disc > staff_disc_max and "discount" not in reasons:
            reasons.append("discount")
        order.approval_for = reasons
        if reasons:
            order.status = OrderStatus.NEEDS_APPROVAL
            if order.reserved_until is None:
                order.reserved_until = datetime.now(tz=UTC) + timedelta(hours=SystemSetting.get_solo().reservation_hours)
            order.accepted_by = None
            order.accepted_at = None
        elif order.status == OrderStatus.NEEDS_APPROVAL:
            order.status = OrderStatus.APPROVED
            order.reserved_until = None
            order.accepted_by = actor
            order.accepted_at = datetime.now(tz=UTC)

    order.save()

    record_audit(actor=actor, action="order.update", entity_type="order", entity_id=order.id)
    publish_order_changed(order.id)
    return get_order_for_actor(actor=actor, order_id=order.id)


@transaction.atomic
def apply_order_discount(*, actor: User, order_id: int, percent: Any) -> dict[str, Any]:
    may_profile_action(actor, "discount")
    order = Order.objects.select_for_update().prefetch_related("lines").filter(id=order_id).first()
    if not order:
        raise ApiException(code=ErrorCode.NOT_FOUND, status_code=http_status.HTTP_404_NOT_FOUND)
    if not can_see_order(actor, order):
        raise ApiException(code=ErrorCode.FORBIDDEN, status_code=http_status.HTTP_403_FORBIDDEN)
    if order.status not in (OrderStatus.PENDING, OrderStatus.NEEDS_APPROVAL, OrderStatus.APPROVED) or hasattr(order, "delivery"):
        raise ApiException(code=ErrorCode.BAD_STATE, message=("لا يمكن تعديل الخصم في هذه المرحلة", "Cannot apply discount at this stage"))

    rules = rules_of()
    disc = _parse_discount_percent(percent, actor.role, rules)
    specs = [{"total": ln.total} for ln in order.lines.all()]
    sub, disc_amt, rnd, tot = _compute_totals(specs, disc)
    now = datetime.now(tz=UTC)
    order.discount_percent = disc
    order.subtotal = sub
    order.discount_amount = disc_amt
    order.discount_by = actor if disc > 0 else None
    order.discount_at = now if disc > 0 else None
    order.rounding = rnd
    order.total = tot

    staff_disc_max = Decimal(str(int(rules.get("staff_discount_max", int(STAFF_DISCOUNT_MAX)))))
    if disc > staff_disc_max and actor.role != Role.MANAGER:
        reasons = list(order.approval_for or [])
        if "discount" not in reasons:
            reasons.append("discount")
        order.approval_for = reasons
        order.status = OrderStatus.NEEDS_APPROVAL
        order.reserved_until = now + timedelta(hours=SystemSetting.get_solo().reservation_hours)

    order.save()
    record_audit(actor=actor, action="order.discount", entity_type="order", entity_id=order.id, after={"discount_percent": float(disc)})
    publish_order_changed(order.id)
    return get_order_for_actor(actor=actor, order_id=order.id)


@transaction.atomic
def cancel_order(*, actor: User, order_id: int, reason: str) -> dict[str, Any]:
    may_profile_action(actor, "cancel_orders")
    order = Order.objects.select_for_update().filter(id=order_id).first()
    if not order:
        raise ApiException(code=ErrorCode.NOT_FOUND, status_code=http_status.HTTP_404_NOT_FOUND)
    if not can_see_order(actor, order):
        raise ApiException(code=ErrorCode.FORBIDDEN, status_code=http_status.HTTP_403_FORBIDDEN)
    if order.status in (OrderStatus.CONFIRMED, OrderStatus.CANCELLED):
        raise ApiException(code=ErrorCode.BAD_STATE, message=("الطلب مكتمل أو ملغي مسبقاً", "Order is already delivered or cancelled"))
    if hasattr(order, "delivery") and order.delivery.status != DeliveryStatus.READY and actor.role != Role.MANAGER:
        raise ApiException(code=ErrorCode.BAD_STATE, message=("لا يمكن إلغاء طلب خرج للتوصيل", "Cannot cancel order out for delivery"))

    reason_clean = (reason or "").strip()
    if not reason_clean:
        msg = ("سبب الإلغاء مطلوب", "Cancellation reason is required")
        raise ApiException(code=ErrorCode.VALIDATION, message=msg, errors={"reason": msg})

    order.status = OrderStatus.CANCELLED
    order.cancel_reason = reason_clean
    order.cancelled_at = datetime.now(tz=UTC)
    order.reserved_until = None
    order.save(update_fields=["status", "cancel_reason", "cancelled_at", "reserved_until"])

    if order.shipment_of_id:
        from apps.deliveries.services import sync_shipments

        sync_shipments(order)

    record_audit(actor=actor, action="order.cancel", entity_type="order", entity_id=order.id, summary=reason_clean)
    publish_order_changed(order.id)
    if order.shipment_of_id:
        publish_order_changed(order.shipment_of_id)
    return get_order_for_actor(actor=actor, order_id=order.id)


@transaction.atomic
def plan_shipments(*, actor: User, order_id: int, loads: Any) -> dict[str, Any]:
    """Assign an order to one vehicle or split a prepared order across multiple vehicles as separate shipments."""
    if actor.role not in (Role.MANAGER, Role.WAREHOUSE, Role.STAFF):
        raise ApiException(code=ErrorCode.FORBIDDEN, status_code=http_status.HTTP_403_FORBIDDEN)

    order = (
        Order.objects.select_for_update()
        .select_related("customer", "delivery", "delivery__rep")
        .prefetch_related("lines__item", "prep_parts")
        .filter(id=order_id)
        .first()
    )
    if not order:
        raise ApiException(code=ErrorCode.NOT_FOUND, status_code=http_status.HTTP_404_NOT_FOUND)

    if actor.role in (Role.WAREHOUSE, Role.STAFF) and not works_in_warehouse(actor, order.warehouse_id):
        if not any(works_in_warehouse(actor, p.warehouse_id) for p in order.prep_parts.all()):
            raise ApiException(
                code=ErrorCode.FORBIDDEN,
                message=("هذا الطلب من مخزن آخر", "This order is from another warehouse"),
                status_code=http_status.HTTP_403_FORBIDDEN,
            )

    dlv = getattr(order, "delivery", None)
    if not dlv or dlv.status != DeliveryStatus.READY:
        raise ApiException(
            code=ErrorCode.BAD_STATE,
            message=("يُوزَّع الطلب على السيارات بعد تجهيزه وقبل خروجه", "An order is put on vehicles after it is prepared and before it leaves"),
            status_code=http_status.HTTP_409_CONFLICT,
        )
    if dlv.loaded_at is not None:
        raise ApiException(
            code=ErrorCode.BAD_STATE,
            message=("حُمّلت الشحنة من قبل: لا يمكن توزيعها الآن", "The shipment is already loaded"),
            status_code=http_status.HTTP_409_CONFLICT,
        )

    if not isinstance(loads, list):
        raise ApiException(code=ErrorCode.VALIDATION, message=("بيانات التوزيع غير صالحة", "Invalid loads payload"))

    clean_loads: list[dict[str, Any]] = []
    for raw_load in loads:
        if not isinstance(raw_load, dict):
            continue
        try:
            vid = int(raw_load.get("vehicle_id"))
        except (TypeError, ValueError) as exc:
            raise ApiException(code=ErrorCode.VALIDATION, message=("معرّف السيارة غير صالح", "Invalid vehicle_id")) from exc
        raw_lines = raw_load.get("lines") or []
        valid_lines: list[dict[str, Any]] = []
        for rl in raw_lines:
            if not isinstance(rl, dict):
                continue
            try:
                qty = int(rl.get("qty") or 0)
                iid = int(rl.get("item_id"))
            except (TypeError, ValueError) as exc:
                raise ApiException(code=ErrorCode.VALIDATION, message=("بيانات المادة غير صالحة", "Invalid load line")) from exc
            unit = rl.get("unit")
            if unit not in ("carton", "piece"):
                raise ApiException(code=ErrorCode.VALIDATION, message=("وحدة غير صالحة", "Invalid unit"))
            if qty > 0:
                valid_lines.append({"item_id": iid, "unit": unit, "qty": qty})
        if valid_lines:
            raw_driver_id = raw_load.get("driver_id")
            clean_loads.append({"vehicle_id": vid, "driver_id": raw_driver_id, "lines": valid_lines})

    if not clean_loads:
        raise ApiException(
            code=ErrorCode.VALIDATION,
            message=("ضع الطلب على سيارة واحدة على الأقل", "Put the order on one vehicle at least"),
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
        )

    seen_vehicles: set[int] = set()
    for ld in clean_loads:
        if ld["vehicle_id"] in seen_vehicles:
            raise ApiException(
                code=ErrorCode.VALIDATION,
                message=("سيارة مكررة: اجمع حمولتها في سطر واحد", "A vehicle twice: put its load in one place"),
                status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            )
        seen_vehicles.add(ld["vehicle_id"])

    order_lines = list(order.lines.order_by("id"))
    order_qty_by_key: dict[tuple[int, str], int] = {}
    base_line_by_key: dict[tuple[int, str], OrderLine] = {}
    for ln in order_lines:
        key = (ln.item_id, ln.unit)
        order_qty_by_key[key] = order_qty_by_key.get(key, 0) + ln.qty
        base_line_by_key.setdefault(key, ln)

    placed_qty_by_key: dict[tuple[int, str], int] = {}
    for ld in clean_loads:
        for xl in ld["lines"]:
            key = (xl["item_id"], xl["unit"])
            if key not in order_qty_by_key:
                raise ApiException(
                    code=ErrorCode.VALIDATION,
                    message=("مادة ليست في الطلب", "An item that isn't in the order"),
                    status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
                )
            placed_qty_by_key[key] = placed_qty_by_key.get(key, 0) + xl["qty"]

    for (iid, unit), req_qty in order_qty_by_key.items():
        put_qty = placed_qty_by_key.get((iid, unit), 0)
        if put_qty != req_qty:
            item_obj = base_line_by_key[(iid, unit)].item
            raise ApiException(
                code=ErrorCode.VALIDATION,
                message=(
                    f"{item_obj.name}: وُزّع {put_qty} من {req_qty}",
                    f"{item_obj.name_en}: {put_qty} of {req_qty} placed",
                ),
                status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

    from apps.catalog.services import item_piece_weight_kg

    vehicles_map = {
        v.id: v
        for v in Vehicle.objects.select_related("driver").filter(id__in=seen_vehicles, active=True)
    }
    for ld in clean_loads:
        v = vehicles_map.get(ld["vehicle_id"])
        if v is None:
            raise ApiException(
                code=ErrorCode.VALIDATION,
                message=("سيارة غير موجودة أو موقوفة", "Unknown or stopped vehicle"),
                status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            )
        load_kg = 0.0
        for xl in ld["lines"]:
            base = base_line_by_key[(xl["item_id"], xl["unit"])]
            pcs = xl["qty"] * base.item.pieces_per_carton if xl["unit"] == "carton" else xl["qty"]
            load_kg += pcs * item_piece_weight_kg(base.item)
        load_kg = round(load_kg * 10.0) / 10.0
        if load_kg > v.capacity_kg:
            raise ApiException(
                code=ErrorCode.VALIDATION,
                message=(
                    f"{v.name} لا تتسع: {int(round(load_kg))} كغم والحمولة القصوى {v.capacity_kg} كغم. أضف سيارة ثانية.",
                    f"{v.name} is full: {int(round(load_kg))} kg for a {v.capacity_kg} kg load. Add another vehicle.",
                ),
                status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            )
        ld["_kg"] = load_kg
        explicit_driver, _ = _resolve_carrier_pick(rep_id=ld.get("driver_id"), vehicle_id=None)
        ld["_explicit_driver"] = explicit_driver

    if len(clean_loads) == 1:
        v = vehicles_map[clean_loads[0]["vehicle_id"]]
        dlv.vehicle = v
        explicit_driver = clean_loads[0]["_explicit_driver"]
        driver = explicit_driver or (
            v.driver if (v.driver and v.driver.active and not v.driver.deleted and v.driver.has_rep_kind("delivery")) else None
        )
        prev_rep_id = dlv.rep_id
        if driver:
            dlv.rep = driver
        dlv.save(update_fields=["vehicle", "rep"])
        if driver and prev_rep_id != driver.id:
            notify_user(
                user_id=driver.id,
                kind="order_accepted",
                title_ar=f"شحنة لك: {order.number}",
                title_en=f"A shipment for you: {order.number}",
                body_ar=f"على {v.name}، جاهزة في المخزن",
                body_en="Ready at the warehouse",
                ref_type="order",
                ref_id=order.id,
            )
        record_audit(
            actor=actor,
            action="update",
            entity_type="order",
            entity_id=order.id,
            summary=f"{order.number} on {v.name}",
            summary_ar=f"{order.number} على {v.name}{f' مع {driver.name}' if driver else ''}",
        )
        publish_order_changed(order.id)
        return get_order_for_actor(actor=actor, order_id=order.id)

    now = datetime.now(tz=UTC)
    created_child_ids: list[int] = []
    prep_parts = list(order.prep_parts.all())
    for k, ld in enumerate(clean_loads):
        v = vehicles_map[ld["vehicle_id"]]
        explicit_driver = ld["_explicit_driver"]
        driver = explicit_driver or (
            v.driver if (v.driver and v.driver.active and not v.driver.deleted and v.driver.has_rep_kind("delivery")) else None
        )
        part_rep = driver or dlv.rep

        child_specs: list[dict[str, Any]] = []
        sub = 0
        for xl in ld["lines"]:
            base = base_line_by_key[(xl["item_id"], xl["unit"])]
            pcs = xl["qty"] * base.item.pieces_per_carton if xl["unit"] == "carton" else xl["qty"]
            line_total = xl["qty"] * base.unit_price
            sub += line_total
            child_specs.append(
                {
                    "item": base.item,
                    "warehouse_id": base.warehouse_id or order.warehouse_id,
                    "unit": xl["unit"],
                    "qty": xl["qty"],
                    "pieces": pcs,
                    "unit_price": base.unit_price,
                    "total": line_total,
                }
            )
        disc_pct = order.discount_percent or Decimal("0")
        disc_amt = calc_percent_round(sub, disc_pct) if disc_pct > 0 else 0
        after_disc = sub - disc_amt
        tot = round_amount_by_rule(after_disc)
        rnd = tot - after_disc

        part_cref = f"{order.client_ref}-s{k + 1}" if order.client_ref else None
        child = Order.objects.create(
            number=f"{order.number}/{k + 1}",
            customer=order.customer,
            warehouse_id=order.warehouse_id,
            rep_id=order.rep_id,
            created_by=actor,
            source=order.source,
            status=OrderStatus.APPROVED,
            notes=order.notes,
            subtotal=sub,
            discount_percent=disc_pct,
            discount_amount=disc_amt,
            discount_by=order.discount_by,
            discount_at=order.discount_at,
            rounding=rnd,
            total=tot,
            accepted_by=order.accepted_by,
            accepted_at=order.accepted_at,
            shipment_of=order,
            shipment_no=k + 1,
            client_ref=part_cref,
        )
        created_child_ids.append(child.id)
        for sp in child_specs:
            OrderLine.objects.create(
                order=child,
                item=sp["item"],
                warehouse_id=sp["warehouse_id"],
                unit=sp["unit"],
                qty=sp["qty"],
                pieces=sp["pieces"],
                unit_price=sp["unit_price"],
                total=sp["total"],
            )
        part_whs = {sp["warehouse_id"] for sp in child_specs}
        for pp in prep_parts:
            if pp.warehouse_id in part_whs:
                PrepPart.objects.create(
                    order=child,
                    warehouse_id=pp.warehouse_id,
                    prepared_by=pp.prepared_by or actor,
                    prepared_at=pp.prepared_at or now,
                )

        pin = _generate_delivery_pin()
        Delivery.objects.create(
            order=child,
            code=_generate_delivery_code(),
            pin_encrypted=encrypt_secret(pin),
            pin_hash=make_password(pin),
            rep=part_rep,
            vehicle=v,
            status=DeliveryStatus.READY,
        )
        if part_rep is not None:
            notify_user(
                user_id=part_rep.id,
                kind="order_accepted",
                title_ar=f"شحنة جديدة لك: {child.number}",
                title_en=f"A new shipment for you: {child.number}",
                body_ar=f"على {v.name} · {int(round(ld['_kg']))} كغم",
                body_en=f"On {v.name}",
                ref_type="order",
                ref_id=child.id,
            )

    dlv.delete()
    if order.reserved_until is not None:
        order.reserved_until = None
        order.save(update_fields=["reserved_until"])

    names = " + ".join(vehicles_map[ld["vehicle_id"]].name for ld in clean_loads)
    record_audit(
        actor=actor,
        action="update",
        entity_type="order",
        entity_id=order.id,
        summary=f"{order.number} split across {len(clean_loads)} vehicles",
        summary_ar=f"توزيع {order.number} على {len(clean_loads)} سيارات: {names}",
        after={"shipments": created_child_ids},
    )
    for ms in order.customer.memberships.all():
        notify_user(
            user_id=ms.user_id,
            kind="order_accepted",
            title_ar=f"طلبك {order.number} يصلك على {len(clean_loads)} دفعات",
            title_en=f"Your order {order.number} comes in {len(clean_loads)} loads",
            body_ar="كل دفعة لها رمز استلام وفاتورة خاصة بها.",
            body_en="Each load has its own PIN and invoice.",
            ref_type="order",
            ref_id=order.id,
        )

    publish_order_changed(order.id)
    for cid in created_child_ids:
        publish_order_changed(cid)
    return get_order_for_actor(actor=actor, order_id=order.id)


@transaction.atomic
def rate_order_delivery(*, actor: User, order_id: int, data: dict[str, Any]) -> dict[str, Any]:
    """Shop owner delivery rating (1-5 stars, optional note) within 7 days of delivery."""
    if actor.role != Role.SHOP:
        raise ApiException(
            code=ErrorCode.FORBIDDEN,
            status_code=http_status.HTTP_403_FORBIDDEN,
            message=("أصحاب المتاجر فقط", "Shop owners only"),
        )
    rules = rules_of()
    if not rules["features"]["ratings"]:
        raise ApiException(
            code=ErrorCode.FEATURE_OFF,
            message=("تقييم التوصيل متوقف", "Delivery ratings are turned off"),
            status_code=http_status.HTTP_403_FORBIDDEN,
        )
    cid = get_active_customer_id(actor)
    order = Order.objects.select_for_update().filter(id=order_id).first()
    if not order or (cid and order.customer_id != cid):
        raise ApiException(code=ErrorCode.NOT_FOUND, status_code=http_status.HTTP_404_NOT_FOUND)

    dlv = getattr(order, "delivery", None)
    if order.status not in ("confirmed", "delivered") and (not dlv or dlv.status != "delivered"):
        raise ApiException(
            code=ErrorCode.BAD_STATE,
            status_code=http_status.HTTP_409_CONFLICT,
            message=("الطلب لم يُسلَّم بعد", "Order isn't delivered yet"),
        )

    now = timezone.now()
    delivered_at = (dlv.delivered_at if dlv and dlv.delivered_at else None) or order.created_at
    if (now - delivered_at).total_seconds() > 7 * 86400:
        raise ApiException(
            code=ErrorCode.BAD_STATE,
            status_code=http_status.HTTP_409_CONFLICT,
            message=("التقييم خلال 7 أيام من التوصيل", "Rating must be within 7 days of delivery"),
        )

    try:
        stars = int(data.get("stars", 0))
        if stars < 1 or stars > 5:
            raise ValueError
    except (TypeError, ValueError):
        raise ApiException(
            code=ErrorCode.VALIDATION,
            message=("التقييم من 1 إلى 5 نجوم", "Stars must be between 1 and 5"),
            errors={"stars": ("من 1 إلى 5", "Between 1 and 5")},
        )

    note = str(data.get("note") or "").strip()[:300]
    first_time = order.rating is None

    rep_obj = None
    if dlv and dlv.rep:
        rep_obj = {"id": dlv.rep.id, "name": dlv.rep.name}
    elif order.rep:
        rep_obj = {"id": order.rep.id, "name": order.rep.name}

    rating_data: dict[str, Any] = {
        "stars": stars,
        "note": note or None,
        "at": now.isoformat(),
    }
    if rep_obj:
        rating_data["rep"] = rep_obj

    order.rating = rating_data
    order.save(update_fields=["rating", "updated_at"])

    shop_name = order.customer.name if order.customer else ""

    record_audit(
        actor=actor,
        action="order.rate_delivery",
        entity_type="order",
        entity_id=order.id,
        summary=f"Delivery rating for {order.number}: {stars}/5" + (f" ({note})" if note else ""),
    )

    low_stars = int(rules.get("low_rating_stars", 2))
    if low_stars > 0 and stars <= low_stars:
        notify_role(
            role=Role.MANAGER,
            kind="needs_approval",
            title_ar=f"تقييم منخفض للتوصيل: {shop_name}",
            title_en=f"Low delivery rating: {shop_name}",
            body_ar=f"{order.number} · {stars} من 5" + (f" · {note}" if note else ""),
            body_en=f"{order.number} · {stars}/5" + (f" · {note}" if note else ""),
            ref_type="order",
            ref_id=order.id,
        )
    elif first_time and stars == 5 and rep_obj:
        notify_user(
            user_id=rep_obj["id"],
            kind="order_confirmed",
            title_ar=f"⭐ 5 من 5 من {shop_name}",
            title_en=f"5/5 from {shop_name}",
            body_ar=note or order.number,
            body_en=note or order.number,
            ref_type="order",
            ref_id=order.id,
        )

    publish_order_changed(order.id)
    return serialize_order(order, actor=actor)

    return get_order_for_actor(actor=actor, order_id=order.id)
