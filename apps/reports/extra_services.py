"""Reorder reminders («قارب ينفد عندك؟») and Day Summary («ملخص اليوم») services."""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from django.db.models import Sum
from rest_framework import status as http_status

from apps.accounts.models import Role, SystemSetting, User
from apps.accounts.permissions import get_active_customer_id, sees_profit
from apps.catalog.models import Item
from apps.common.exceptions import ApiException, ErrorCode
from apps.common.utils import BAGHDAD_TZ, baghdad_now, baghdad_today
from apps.customers.models import Customer
from apps.inventory.models import Batch
from apps.invoices.models import Invoice, ReturnDoc
from apps.orders.models import Order, OrderLine, OrderStatus
from apps.payments.models import Payment


def get_my_reorder_due(*, actor: User) -> list[dict[str, Any]]:
    if actor.role != Role.SHOP:
        raise ApiException(code=ErrorCode.FORBIDDEN, status_code=http_status.HTTP_403_FORBIDDEN)
    cid = get_active_customer_id(actor)
    if not cid:
        return []

    today = baghdad_today()
    stocked_items = set(
        Batch.objects.filter(qty_left__gt=0, held_at__isnull=True, expiry_date__gt=today)
        .values("item_id")
        .annotate(total=Sum("qty_left"))
        .filter(total__gt=0)
        .values_list("item_id", flat=True)
    )

    lines = list(
        OrderLine.objects.select_related("order", "item")
        .filter(order__customer_id=cid, item__active=True, item__deleted=False)
        .exclude(order__status=OrderStatus.CANCELLED)
        .order_by("order__created_at", "id")
    )

    by_item: dict[int, list[OrderLine]] = {}
    for ln in lines:
        by_item.setdefault(ln.item_id, []).append(ln)

    out: list[dict[str, Any]] = []
    for iid, item_lines in by_item.items():
        if iid not in stocked_items:
            continue
        seen_days: list[date] = []
        for ln in item_lines:
            d = ln.order.created_at.astimezone(BAGHDAD_TZ).date()
            if not seen_days or seen_days[-1] != d:
                seen_days.append(d)
        if len(seen_days) < 2:
            continue
        gaps = [(seen_days[i] - seen_days[i - 1]).days for i in range(1, len(seen_days))]
        avg_gap = max(1, round(sum(gaps) / len(gaps)))
        last_ln = item_lines[-1]
        last_day = last_ln.order.created_at.astimezone(BAGHDAD_TZ).date()
        days_since = (today - last_day).days
        if days_since >= max(1, round(avg_gap * 0.8)):
            out.append(
                {
                    "item_id": iid,
                    "every_days": avg_gap,
                    "days_since": days_since,
                    "last_at": last_ln.order.created_at.isoformat(),
                    "last_unit": last_ln.unit,
                    "last_qty": last_ln.qty,
                }
            )

    out.sort(key=lambda r: (r["days_since"] / max(1, r["every_days"])), reverse=True)
    return out


def get_late_shops(*, actor: User) -> list[dict[str, Any]]:
    if actor.role == Role.REP:
        cust_qs = Customer.objects.filter(active=True, walk_in=False, rep_id=actor.id)
    elif actor.role in (Role.MANAGER, Role.ACCOUNTANT):
        cust_qs = Customer.objects.filter(active=True, walk_in=False)
    else:
        raise ApiException(code=ErrorCode.FORBIDDEN, status_code=http_status.HTTP_403_FORBIDDEN)

    customers = {c.id: c for c in cust_qs}
    if not customers:
        return []

    today = baghdad_today()
    orders = list(
        Order.objects.filter(customer_id__in=list(customers.keys()))
        .exclude(status=OrderStatus.CANCELLED)
        .order_by("created_at", "id")
    )
    by_cust: dict[int, list[Order]] = {}
    for o in orders:
        by_cust.setdefault(o.customer_id, []).append(o)

    out: list[dict[str, Any]] = []
    for cid, cust in customers.items():
        cust_orders = by_cust.get(cid, [])
        seen_days: list[date] = []
        for o in cust_orders:
            d = o.created_at.astimezone(BAGHDAD_TZ).date()
            if not seen_days or seen_days[-1] != d:
                seen_days.append(d)
        if len(seen_days) < 2:
            continue
        gaps = [(seen_days[i] - seen_days[i - 1]).days for i in range(1, len(seen_days))]
        avg_gap = max(1, round(sum(gaps) / len(gaps)))
        last_o = cust_orders[-1]
        last_day = last_o.created_at.astimezone(BAGHDAD_TZ).date()
        days_since = (today - last_day).days
        if days_since > avg_gap:
            out.append(
                {
                    "customer": {
                        "id": cust.id,
                        "name": cust.name,
                        "area": cust.area,
                        "phone": cust.phone,
                        "rep_id": cust.rep_id or 0,
                    },
                    "every_days": avg_gap,
                    "days_since": days_since,
                    "last_at": last_o.created_at.isoformat(),
                }
            )

    out.sort(key=lambda r: (r["days_since"] - r["every_days"]), reverse=True)
    return out


def _compute_day_numbers(
    target_day: date,
    *,
    invoices: list[Invoice],
    returns: list[ReturnDoc],
    payments: list[Payment],
    orders: list[Order],
    include_profit: bool,
) -> dict[str, Any]:
    day_invs = [inv for inv in invoices if inv.delivered_at.astimezone(BAGHDAD_TZ).date() == target_day]
    day_rets = [r for r in returns if r.created_at.astimezone(BAGHDAD_TZ).date() == target_day]
    day_pays = [p for p in payments if p.created_at.astimezone(BAGHDAD_TZ).date() == target_day]
    day_orders = [o for o in orders if o.created_at.astimezone(BAGHDAD_TZ).date() == target_day]

    sales = sum(inv.total for inv in day_invs)
    ret_total = sum(r.amount for r in day_rets)
    collected = sum((-p.amount if p.reversal_of_id else p.amount) for p in day_pays)
    out: dict[str, Any] = {
        "sales": sales,
        "invoices": len(day_invs),
        "returns": ret_total,
        "collected": collected,
        "new_debt": sales - ret_total - collected,
        "orders": len(day_orders),
    }
    if include_profit:
        out["profit"] = sum(inv.profit for inv in day_invs)
    return out


def build_day_summary(*, actor: User, date_str: str | None) -> dict[str, Any]:
    if actor.role not in (Role.MANAGER, Role.ACCOUNTANT):
        raise ApiException(code=ErrorCode.FORBIDDEN, status_code=http_status.HTTP_403_FORBIDDEN)

    today = baghdad_today()
    if date_str:
        try:
            target_day = date.fromisoformat(date_str.strip())
        except ValueError as exc:
            raise ApiException(code=ErrorCode.VALIDATION, message=("التاريخ غير صحيح", "Invalid date")) from exc
    else:
        target_day = today

    yesterday = target_day - timedelta(days=1)
    last_week = target_day - timedelta(days=7)

    # Fetch 10-day window around target_day
    win_start = baghdad_now().astimezone(BAGHDAD_TZ).replace(year=last_week.year, month=last_week.month, day=last_week.day, hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
    win_end = baghdad_now().astimezone(BAGHDAD_TZ).replace(year=target_day.year, month=target_day.month, day=target_day.day, hour=23, minute=59, second=59, microsecond=999999) + timedelta(days=1)

    invoices = list(Invoice.objects.select_related("rep").filter(delivered_at__gte=win_start, delivered_at__lte=win_end))
    returns = list(ReturnDoc.objects.filter(created_at__gte=win_start, created_at__lte=win_end))
    payments = list(Payment.objects.filter(created_at__gte=win_start, created_at__lte=win_end))
    orders = list(Order.objects.filter(created_at__gte=win_start, created_at__lte=win_end))

    include_profit = sees_profit(actor)
    day_nums = _compute_day_numbers(target_day, invoices=invoices, returns=returns, payments=payments, orders=orders, include_profit=include_profit)
    yest_nums = _compute_day_numbers(yesterday, invoices=invoices, returns=returns, payments=payments, orders=orders, include_profit=include_profit)
    week_nums = _compute_day_numbers(last_week, invoices=invoices, returns=returns, payments=payments, orders=orders, include_profit=include_profit)

    day_invs = [inv for inv in invoices if inv.delivered_at.astimezone(BAGHDAD_TZ).date() == target_day]
    counter_sales = sum(inv.total for inv in day_invs if inv.counter)

    rep_totals: dict[int, tuple[str, int]] = {}
    for inv in day_invs:
        if inv.rep_id:
            prev_name, prev_val = rep_totals.get(inv.rep_id, (inv.rep.name, 0))
            rep_totals[inv.rep_id] = (prev_name, prev_val + inv.total)
    best_rep = None
    if rep_totals:
        top_id, (top_name, top_sales) = max(rep_totals.items(), key=lambda kv: kv[1][1])
        best_rep = {"id": top_id, "name": top_name, "sales": top_sales}

    cutoff_late = baghdad_now() - timedelta(hours=24)
    late_orders = list(
        Order.objects.select_related("customer")
        .filter(status__in=[OrderStatus.PENDING, OrderStatus.NEEDS_APPROVAL, OrderStatus.APPROVED], created_at__lte=cutoff_late)
        .order_by("created_at")[:50]
    )
    late_out = [
        {
            "order_id": o.id,
            "number": o.number,
            "customer": o.customer.name,
            "since": o.created_at.isoformat(),
        }
        for o in late_orders
    ]

    stock_by_item = {
        row["item_id"]: int(row["total"] or 0)
        for row in Batch.objects.filter(qty_left__gt=0, held_at__isnull=True, expiry_date__gt=today)
        .values("item_id")
        .annotate(total=Sum("qty_left"))
    }
    low_out = []
    for it in Item.objects.filter(active=True, deleted=False, min_stock__gt=0).order_by("id"):
        qty = stock_by_item.get(it.id, 0)
        if qty < it.min_stock:
            low_out.append(
                {
                    "item_id": it.id,
                    "name": it.name,
                    "name_en": it.name_en,
                    "qty": qty,
                    "min": it.min_stock,
                }
            )

    summary_hour = SystemSetting.get_solo().summary_hour
    out: dict[str, Any] = {
        "date": target_day.isoformat(),
        "day": day_nums,
        "yesterday": yest_nums,
        "last_week": week_nums,
        "counter_sales": counter_sales,
        "late": late_out,
        "low": low_out,
        "hour": summary_hour,
    }
    if best_rep:
        out["best_rep"] = best_rep
    return out
