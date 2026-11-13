"""Dashboard KPI and analytical reporting services with strict RBAC scoping and financial redactions."""
from __future__ import annotations

import re
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from django.db.models import Q, Sum
from rest_framework import status as http_status

from apps.accounts.models import Role, SystemSetting, User
from apps.accounts.permissions import own_warehouse_id, sees_debt, sees_profit
from apps.catalog.models import Item
from apps.common.exceptions import ApiException, ErrorCode
from apps.common.utils import baghdad_day_of, baghdad_today
from apps.customers.models import Customer, CustomerStatus
from apps.customers.services import customer_balance_map, last_order_at_map, serialize_customer
from apps.inventory.models import Batch
from apps.inventory.serializers import serialize_batch
from apps.inventory.services import shelf_available
from apps.invoices.models import Invoice, InvoiceLine, ReturnDoc
from apps.orders.models import Order, OrderStatus
from apps.payments.models import Payment


def _resolve_wh_scope(actor: User, warehouse_id: int | None) -> int | None:
    own_wh = own_warehouse_id(actor)
    if own_wh is not None:
        return own_wh
    return warehouse_id


def get_dashboard(*, actor: User, warehouse_id: int | None = None) -> dict[str, Any]:
    if actor.role == Role.SHOP:
        raise ApiException(code=ErrorCode.FORBIDDEN, status_code=http_status.HTTP_403_FORBIDDEN)

    today = baghdad_today()
    today_str = today.isoformat()
    month_prefix = today_str[:7]
    near_days = SystemSetting.get_solo().near_expiry_days
    near_cutoff = today + timedelta(days=near_days)

    sc_wh = _resolve_wh_scope(actor, warehouse_id)

    order_qs = Order.objects.all()
    inv_qs = Invoice.objects.prefetch_related("returns").all()
    ret_qs = ReturnDoc.objects.all()
    batch_qs = Batch.objects.all()
    if sc_wh is not None:
        order_qs = order_qs.filter(Q(warehouse_id=sc_wh) | Q(lines__warehouse_id=sc_wh)).distinct()
        inv_qs = inv_qs.filter(Q(warehouse_id=sc_wh) | Q(lines__warehouse_id=sc_wh)).distinct()
        ret_qs = ret_qs.filter(Q(warehouse_id=sc_wh) | Q(invoice__warehouse_id=sc_wh)).distinct()
        batch_qs = batch_qs.filter(warehouse_id=sc_wh)
    elif actor.role == Role.REP:
        order_qs = order_qs.filter(
            Q(rep_id=actor.id) | Q(customer__rep_id=actor.id) | Q(delivery__rep_id=actor.id)
        ).distinct()
        inv_qs = inv_qs.filter(
            Q(rep_id=actor.id) | Q(delivery_rep_id=actor.id) | Q(customer__rep_id=actor.id)
        ).distinct()
        ret_qs = ret_qs.filter(Q(rep_id=actor.id) | Q(customer__rep_id=actor.id)).distinct()

    pending_orders = order_qs.filter(status=OrderStatus.PENDING).count()
    needs_approval = order_qs.filter(status=OrderStatus.NEEDS_APPROVAL).count()

    last7_days = [(today - timedelta(days=6 - k)).isoformat() for k in range(7)]
    sales_by_day: dict[str, int] = {ds: 0 for ds in last7_days}

    today_sales = 0
    today_invoices = 0
    month_inv_sales = 0
    month_inv_profit = 0
    for inv in inv_qs.only("id", "delivered_at", "total", "cost"):
        d = baghdad_day_of(inv.delivered_at)
        if d in sales_by_day:
            sales_by_day[d] += inv.total
        if d == today_str:
            today_sales += inv.total
            today_invoices += 1
        if d.startswith(month_prefix):
            month_inv_sales += inv.total
            recovered_total = sum((r.cost_recovered for r in inv.returns.all()), 0)
            orig_cost = inv.cost + recovered_total
            month_inv_profit += inv.total - orig_cost

    month_ret_sales = 0
    month_ret_profit = 0
    for ret in ret_qs.only("id", "created_at", "amount", "cost_recovered"):
        d = baghdad_day_of(ret.created_at)
        if d.startswith(month_prefix):
            month_ret_sales += ret.amount
            month_ret_profit += ret.amount - ret.cost_recovered

    month_sales = month_inv_sales - month_ret_sales
    month_profit = month_inv_profit - month_ret_profit

    near_expiry = batch_qs.filter(
        qty_left__gt=0, held_at__isnull=True, expiry_date__gte=today, expiry_date__lte=near_cutoff
    ).count()
    expired_batches = batch_qs.filter(qty_left__gt=0, expiry_date__lt=today).count()

    low_stock = 0
    for item in Item.objects.filter(active=True, min_stock__gt=0).only("id", "min_stock"):
        if sc_wh is not None:
            avail = shelf_available(sc_wh, item.id)
            thresh = Decimal(item.min_stock) / Decimal("2")
        else:
            avail = (
                batch_qs.filter(item=item, qty_left__gt=0, held_at__isnull=True, expiry_date__gte=today)
                .aggregate(s=Sum("qty_left"))["s"]
                or 0
            )
            thresh = Decimal(item.min_stock)
        if Decimal(avail) < thresh:
            low_stock += 1

    can_debt = sees_debt(actor)
    collected_by_day: dict[str, int] = {ds: 0 for ds in last7_days}
    today_collected = 0
    total_debt = 0
    over_limit_customers = 0

    if can_debt:
        pay_qs = Payment.objects.filter(reversed=False, reversal_of__isnull=True, amount__gt=0)
        cust_qs = Customer.objects.filter(active=True, status=CustomerStatus.ACTIVE)
        if actor.role == Role.REP:
            pay_qs = pay_qs.filter(Q(customer__rep_id=actor.id) | Q(received_by=actor))
            cust_qs = cust_qs.filter(rep_id=actor.id)

        for p in pay_qs.only("amount", "created_at"):
            d = baghdad_day_of(p.created_at)
            if d == today_str:
                today_collected += p.amount
            if d in collected_by_day:
                collected_by_day[d] += p.amount

        cust_ids = list(cust_qs.values_list("id", flat=True))
        bmap = customer_balance_map(cust_ids)
        total_debt = sum(max(0, b) for b in bmap.values())
        for c in cust_qs.filter(debt_limit__gt=0).only("id", "debt_limit"):
            if bmap.get(c.id, 0) > c.debt_limit:
                over_limit_customers += 1

    out: dict[str, Any] = {
        "today_sales": today_sales,
        "today_invoices": today_invoices,
        "today_collected": today_collected,
        "month_sales": month_sales,
        "total_debt": total_debt,
        "over_limit_customers": over_limit_customers,
        "pending_orders": pending_orders,
        "needs_approval": needs_approval,
        "low_stock": low_stock,
        "near_expiry": near_expiry,
        "expired_batches": expired_batches,
        "last7": [
            {"date": ds, "sales": sales_by_day[ds], "collected": collected_by_day[ds]}
            for ds in last7_days
        ],
    }

    if sees_profit(actor):
        from apps.expenses.services import month_expenses_total

        month_exp = month_expenses_total(month_prefix, warehouse_id=sc_wh)
        out["month_profit"] = month_profit
        out["month_expenses"] = month_exp
        out["month_net"] = month_profit - month_exp

    return out


def _build_sales_rows(
    *,
    actor: User,
    group_by: str,
    date_from: str | None,
    date_to: str | None,
    warehouse_id: int | None,
) -> list[dict[str, Any]]:
    gb = (group_by or "item").strip().lower()
    if gb not in ("rep", "item", "warehouse", "customer"):
        gb = "item"

    can_profit = sees_profit(actor)
    inv_qs = (
        Invoice.objects.select_related("customer", "rep", "delivery_rep", "warehouse")
        .prefetch_related("lines__item", "lines__warehouse", "returns")
        .all()
    )
    ret_qs = (
        ReturnDoc.objects.select_related("invoice", "invoice__rep", "invoice__warehouse", "customer", "rep", "warehouse")
        .prefetch_related("lines__item")
        .all()
    )
    if warehouse_id is not None:
        inv_qs = inv_qs.filter(Q(warehouse_id=warehouse_id) | Q(lines__warehouse_id=warehouse_id)).distinct()
        ret_qs = ret_qs.filter(Q(warehouse_id=warehouse_id) | Q(invoice__warehouse_id=warehouse_id)).distinct()
    if actor.role == Role.REP:
        inv_qs = inv_qs.filter(
            Q(rep_id=actor.id) | Q(delivery_rep_id=actor.id) | Q(customer__rep_id=actor.id)
        ).distinct()
        ret_qs = ret_qs.filter(Q(rep_id=actor.id) | Q(customer__rep_id=actor.id)).distinct()

    buckets: dict[str, dict[str, Any]] = {}

    def _bucket(key: str, label: str, label_en: str) -> dict[str, Any]:
        return buckets.setdefault(
            key,
            {
                "key": key,
                "label": label,
                "label_en": label_en,
                "invoices": 0,
                "pieces": 0,
                "sales": 0,
                "returns": 0,
                "profit": 0,
            },
        )

    for inv in inv_qs:
        d = baghdad_day_of(inv.delivered_at)
        if date_from and d < date_from:
            continue
        if date_to and d > date_to:
            continue
        if gb == "item":
            for ln in inv.lines.all():
                if warehouse_id is not None and ln.warehouse_id != warehouse_id:
                    continue
                b = _bucket(str(ln.item_id), ln.item.name, ln.item.name_en)
                b["invoices"] += 1
                b["pieces"] += ln.pieces
                b["sales"] += ln.total
                b["profit"] += ln.total - ln.cost
        else:
            if gb == "rep":
                key_id = inv.rep_id or 0
                label = inv.rep.name if inv.rep else "-"
                label_en = inv.rep.name if inv.rep else "-"
            elif gb == "warehouse":
                key_id = inv.warehouse_id
                label = inv.warehouse.name
                label_en = inv.warehouse.name_en
            else:
                key_id = inv.customer_id
                label = inv.customer.name
                label_en = inv.customer.name
            b = _bucket(str(key_id), label, label_en)
            b["invoices"] += 1
            b["pieces"] += sum((ln.pieces for ln in inv.lines.all()), 0)
            b["sales"] += inv.total
            recovered = sum((r.cost_recovered for r in inv.returns.all()), 0)
            b["profit"] += inv.total - (inv.cost + recovered)

    for ret in ret_qs:
        d = baghdad_day_of(ret.created_at)
        if date_from and d < date_from:
            continue
        if date_to and d > date_to:
            continue
        inv = ret.invoice
        if warehouse_id is not None and inv and inv.warehouse_id != warehouse_id and ret.warehouse_id != warehouse_id:
            continue
        if gb == "item":
            for rl in ret.lines.all():
                b = _bucket(str(rl.item_id), rl.item.name, rl.item.name_en)
                b["returns"] += rl.amount
                b["profit"] -= rl.amount - rl.cost
        else:
            if gb == "rep":
                rep_obj = ret.rep or (inv.rep if inv else None)
                key_id = rep_obj.id if rep_obj else 0
                label = rep_obj.name if rep_obj else "-"
                label_en = rep_obj.name if rep_obj else "-"
            elif gb == "warehouse":
                wh_obj = inv.warehouse if inv else ret.warehouse
                key_id = wh_obj.id
                label = wh_obj.name
                label_en = wh_obj.name_en
            else:
                cust_obj = inv.customer if inv else ret.customer
                key_id = cust_obj.id
                label = cust_obj.name
                label_en = cust_obj.name
            b = _bucket(str(key_id), label, label_en)
            b["returns"] += ret.amount
            b["profit"] -= ret.amount - ret.cost_recovered

    rows: list[dict[str, Any]] = []
    for b in sorted(buckets.values(), key=lambda x: -x["sales"]):
        row: dict[str, Any] = {
            "key": b["key"],
            "label": b["label"],
            "label_en": b["label_en"],
            "invoices": b["invoices"],
            "pieces": b["pieces"],
            "sales": b["sales"],
            "returns": b["returns"],
        }
        if can_profit:
            row["profit"] = b["profit"]
        rows.append(row)
    return rows


def get_daily_sales(*, actor: User, date_str: str | None = None, warehouse_id: int | None = None) -> dict[str, Any]:
    if actor.role == Role.SHOP:
        raise ApiException(code=ErrorCode.FORBIDDEN, status_code=http_status.HTTP_403_FORBIDDEN)

    target_date = (date_str or "").strip() or baghdad_today().isoformat()
    sc_wh = _resolve_wh_scope(actor, warehouse_id)

    by_rep = _build_sales_rows(
        actor=actor, group_by="rep", date_from=target_date, date_to=target_date, warehouse_id=sc_wh
    )
    by_warehouse = _build_sales_rows(
        actor=actor, group_by="warehouse", date_from=target_date, date_to=target_date, warehouse_id=sc_wh
    )

    sales = sum((r["sales"] for r in by_warehouse), 0)
    invoices_count = sum((r["invoices"] for r in by_warehouse), 0)
    returns_total = sum((r["returns"] for r in by_warehouse), 0)

    collected = 0
    if sees_debt(actor):
        pay_qs = Payment.objects.filter(reversed=False, reversal_of__isnull=True, amount__gt=0)
        if actor.role == Role.REP:
            pay_qs = pay_qs.filter(Q(customer__rep_id=actor.id) | Q(received_by=actor))
        collected = sum(
            (p.amount for p in pay_qs.only("amount", "created_at") if baghdad_day_of(p.created_at) == target_date),
            0,
        )

    return {
        "date": target_date,
        "sales": sales,
        "invoices": invoices_count,
        "collected": collected,
        "returns": returns_total,
        "by_rep": by_rep,
        "by_warehouse": by_warehouse,
    }


def get_sales_report(
    *,
    actor: User,
    group_by: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    warehouse_id: int | None = None,
) -> list[dict[str, Any]]:
    if actor.role not in (Role.MANAGER, Role.ACCOUNTANT, Role.REP):
        raise ApiException(code=ErrorCode.FORBIDDEN, status_code=http_status.HTTP_403_FORBIDDEN)

    return _build_sales_rows(
        actor=actor,
        group_by=group_by or "item",
        date_from=date_from,
        date_to=date_to,
        warehouse_id=warehouse_id,
    )


def get_slow_moving_report(*, actor: User, days: int = 30, warehouse_id: int | None = None) -> list[dict[str, Any]]:
    if actor.role not in (Role.MANAGER, Role.ACCOUNTANT, Role.WAREHOUSE, Role.STAFF):
        raise ApiException(code=ErrorCode.FORBIDDEN, status_code=http_status.HTTP_403_FORBIDDEN)

    today = baghdad_today()
    min_days = max(1, days)
    sc_wh = _resolve_wh_scope(actor, warehouse_id)

    line_qs = InvoiceLine.objects.select_related("invoice").all()
    if sc_wh is not None:
        line_qs = line_qs.filter(warehouse_id=sc_wh)

    last_sold_map: dict[int, date] = {}
    for ln in line_qs.only("item_id", "invoice__delivered_at"):
        d = date.fromisoformat(baghdad_day_of(ln.invoice.delivered_at))
        if ln.item_id not in last_sold_map or d > last_sold_map[ln.item_id]:
            last_sold_map[ln.item_id] = d

    batch_base = Batch.objects.filter(qty_left__gt=0, held_at__isnull=True, expiry_date__gte=today)
    if sc_wh is not None:
        batch_base = batch_base.filter(warehouse_id=sc_wh)

    stock_by_item = {
        row["item_id"]: (row["s"] or 0)
        for row in batch_base.values("item_id").annotate(s=Sum("qty_left"))
    }

    rows: list[dict[str, Any]] = []
    for item_id in Item.objects.filter(active=True).order_by("id").values_list("id", flat=True):
        stock = stock_by_item.get(item_id, 0)
        if stock <= 0:
            continue
        last_sold = last_sold_map.get(item_id)
        days_idle = (today - last_sold).days if last_sold else None
        if days_idle is not None and days_idle < min_days:
            continue
        row: dict[str, Any] = {
            "item_id": item_id,
            "days": days_idle,
            "stock": stock,
        }
        if last_sold is not None:
            row["last_sale"] = last_sold.isoformat()
        rows.append(row)

    rows.sort(key=lambda r: -(r["days"] if r["days"] is not None else 999999))
    return rows


def get_near_expiry_report(*, actor: User, days: int | None = None, warehouse_id: int | None = None) -> list[dict[str, Any]]:
    if actor.role not in (Role.MANAGER, Role.ACCOUNTANT, Role.WAREHOUSE, Role.STAFF):
        raise ApiException(code=ErrorCode.FORBIDDEN, status_code=http_status.HTTP_403_FORBIDDEN)

    today = baghdad_today()
    window = days if (days is not None and days > 0) else SystemSetting.get_solo().near_expiry_days
    cutoff = today + timedelta(days=window)
    sc_wh = _resolve_wh_scope(actor, warehouse_id)

    qs = (
        Batch.objects.select_related("item", "warehouse", "held_by")
        .filter(qty_left__gt=0, held_at__isnull=True, expiry_date__gte=today, expiry_date__lte=cutoff)
        .order_by("expiry_date", "id")
    )
    if sc_wh is not None:
        qs = qs.filter(warehouse_id=sc_wh)

    return [
        {
            "batch": serialize_batch(b, actor=actor),
            "days_left": (b.expiry_date - today).days,
        }
        for b in qs
    ]


def get_debts_report(*, actor: User) -> list[dict[str, Any]]:
    if actor.role not in (Role.MANAGER, Role.ACCOUNTANT, Role.REP):
        raise ApiException(code=ErrorCode.FORBIDDEN, status_code=http_status.HTTP_403_FORBIDDEN)

    cust_qs = Customer.objects.select_related("rep", "tier").filter(active=True, status=CustomerStatus.ACTIVE)
    if actor.role == Role.REP:
        cust_qs = cust_qs.filter(rep_id=actor.id)

    customers = list(cust_qs)
    cids = [c.id for c in customers]
    bmap = customer_balance_map(cids)
    lmap = last_order_at_map(cids)

    oldest_open: dict[int, str] = {}
    open_count: dict[int, int] = {}
    for inv in Invoice.objects.filter(customer_id__in=cids).only(
        "customer_id", "total", "paid", "returned", "credited", "delivered_at"
    ):
        if inv.remaining > 0:
            open_count[inv.customer_id] = open_count.get(inv.customer_id, 0) + 1
            iso = inv.delivered_at.isoformat()
            if inv.customer_id not in oldest_open or iso < oldest_open[inv.customer_id]:
                oldest_open[inv.customer_id] = iso

    rows: list[dict[str, Any]] = []
    for c in customers:
        bal = bmap.get(c.id, 0)
        if bal <= 0:
            continue
        cust_payload = serialize_customer(c, actor=actor, balance_map=bmap, last_order_map=lmap)
        row: dict[str, Any] = {
            "customer": cust_payload,
            "open_invoices": open_count.get(c.id, 0),
        }
        if c.id in oldest_open:
            row["oldest_open"] = oldest_open[c.id]
        rows.append(row)

    rows.sort(key=lambda r: -(r["customer"].get("balance") or 0))
    return rows


_MONTH_RE = re.compile(r"^\d{4}-\d{2}$")


def get_rep_targets(*, actor: User, month: str | None = None) -> list[dict[str, Any]]:
    """Return each marketing rep's monthly sales minus returns against sales_target."""
    if actor.role not in (Role.MANAGER, Role.ACCOUNTANT, Role.REP):
        raise ApiException(code=ErrorCode.FORBIDDEN, status_code=http_status.HTTP_403_FORBIDDEN)

    month_str = (month or "").strip() or baghdad_today().strftime("%Y-%m")
    if not _MONTH_RE.match(month_str):
        raise ApiException(
            code=ErrorCode.VALIDATION,
            status_code=http_status.HTTP_400_BAD_REQUEST,
            errors={"month": "YYYY-MM required"},
        )

    reps = [
        u
        for u in User.objects.filter(role=Role.REP, active=True, deleted=False).order_by("id")
        if u.has_rep_kind("sales")
    ]
    sales_by_rep: dict[int, int] = {u.id: 0 for u in reps}
    inv_count_by_rep: dict[int, int] = {u.id: 0 for u in reps}
    returns_by_rep: dict[int, int] = {u.id: 0 for u in reps}

    rep_ids = set(sales_by_rep.keys())
    if rep_ids:
        for inv in Invoice.objects.filter(rep_id__in=rep_ids).only("id", "rep_id", "delivered_at", "total"):
            if baghdad_day_of(inv.delivered_at).startswith(month_str):
                sales_by_rep[inv.rep_id] += int(inv.total)
                inv_count_by_rep[inv.rep_id] += 1

        for ret in ReturnDoc.objects.select_related("invoice").filter(invoice__rep_id__in=rep_ids).only(
            "id", "amount", "created_at", "invoice__rep_id"
        ):
            if baghdad_day_of(ret.created_at).startswith(month_str):
                returns_by_rep[ret.invoice.rep_id] += int(ret.amount)

    rows: list[dict[str, Any]] = []
    for u in reps:
        sales = sales_by_rep[u.id]
        returns = returns_by_rep[u.id]
        net = sales - returns
        target = int(u.sales_target or 0)
        pct = (
            int((Decimal(net) * Decimal(100) / Decimal(target)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
            if target > 0
            else 0
        )
        rows.append(
            {
                "rep": {"id": u.id, "name": u.name},
                "month": month_str,
                "target": target,
                "sales": sales,
                "returns": returns,
                "net": net,
                "invoices": inv_count_by_rep[u.id],
                "pct": pct,
                "rank": 0,
                "of": 0,
            }
        )

    rows.sort(key=lambda r: (-(1 if r["target"] else 0), -r["pct"], -r["net"], r["rep"]["id"]))
    total_reps = len(rows)
    for idx, row in enumerate(rows):
        row["rank"] = idx + 1
        row["of"] = total_reps

    if actor.role == Role.REP:
        return [r for r in rows if r["rep"]["id"] == actor.id]
    return rows


def delivery_ratings_report(*, actor: User) -> dict[str, Any]:
    """Aggregate customer ratings of completed deliveries for manager and accountant."""
    if actor.role not in (Role.MANAGER, Role.ACCOUNTANT):
        raise ApiException(
            code=ErrorCode.FORBIDDEN,
            status_code=http_status.HTTP_403_FORBIDDEN,
            message=("المدير والمحاسب فقط", "Manager and accountant only"),
        )
    from apps.orders.models import Order

    rated_orders = list(Order.objects.filter(rating__isnull=False).select_related("customer").order_by("-id"))

    def _rating_at(o):
        return (o.rating or {}).get("at") or ""

    rated_orders.sort(key=_rating_at, reverse=True)

    count = len(rated_orders)
    spread = [0, 0, 0, 0, 0]
    all_stars = []
    by_rep: dict[int, dict[str, Any]] = {}

    for o in rated_orders:
        r = o.rating or {}
        stars = int(r.get("stars") or 0)
        if 1 <= stars <= 5:
            spread[stars - 1] += 1
            all_stars.append(stars)
            rep_info = r.get("rep")
            if rep_info and isinstance(rep_info, dict) and "id" in rep_info:
                rid = int(rep_info["id"])
                if rid not in by_rep:
                    by_rep[rid] = {"rep": rep_info, "stars": []}
                by_rep[rid]["stars"].append(stars)

    def _avg(xs: list[int]) -> float:
        return round(sum(xs) / len(xs), 1) if xs else 0.0

    avg_overall = _avg(all_stars)
    reps_list = []
    for _rid, entry in by_rep.items():
        s_list = entry["stars"]
        reps_list.append(
            {
                "rep": entry["rep"],
                "count": len(s_list),
                "average": _avg(s_list),
                "low": sum(1 for s in s_list if s <= 2),
            }
        )
    reps_list.sort(key=lambda x: x["average"], reverse=True)

    latest_list = []
    for o in rated_orders[:30]:
        latest_list.append(
            {
                "order_id": o.id,
                "number": o.number,
                "customer_id": o.customer_id,
                "customer": o.customer.name if o.customer else "",
                "rating": o.rating,
            }
        )

    return {
        "count": count,
        "average": avg_overall,
        "spread": spread,
        "reps": reps_list,
        "latest": latest_list,
    }

