"""Commission calculation, monthly snapshot closing, and line-level breakdown services."""
from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from django.db import transaction
from rest_framework import status as http_status

from apps.accounts.models import Role, SystemSetting, User
from apps.audit.services import record_audit
from apps.commissions.models import ClosedCommissionMonth
from apps.common.exceptions import ApiException, ErrorCode
from apps.common.utils import baghdad_day_of, baghdad_today
from apps.invoices.models import Invoice, ReturnDoc
from apps.notifications.services import notify_role, notify_user
from apps.payments.models import PaymentAllocation

_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def _normalize_month(raw: str | None) -> str:
    m = (raw or "").strip() or baghdad_today().strftime("%Y-%m")
    if not _MONTH_RE.match(m):
        msg = ("صيغة الشهر يجب أن تكون YYYY-MM", "Month must be in YYYY-MM format")
        raise ApiException(code=ErrorCode.VALIDATION, message=msg, errors={"month": msg})
    return m


def _round_iqd(val: Decimal) -> int:
    return int(val.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _fmt_rate_pct(rate_frac: Decimal) -> int | float:
    pct = (rate_frac * Decimal("100")).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    return int(pct) if pct == int(pct) else float(pct)


def _invoice_rate(inv: Invoice) -> Decimal:
    """Weighted commission rate fraction of an invoice from its items' commission_percent."""
    lines = list(inv.lines.all())
    total_val = sum((ln.total for ln in lines), 0)
    if total_val <= 0:
        return Decimal("0")
    weighted = sum(
        (Decimal(ln.total) * (Decimal(ln.item.commission_percent or 0) / Decimal("100")) for ln in lines),
        Decimal("0"),
    )
    return weighted / Decimal(total_val)


def _compute_live_month(month: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Compute rep CommissionRow[] and CommissionLine[] for a month using weighted Item rates."""
    by_rep: dict[int, dict[str, Any]] = {
        u.id: {
            "id": u.id,
            "name": u.name,
            "paid_sales": 0,
            "commission_dec": Decimal("0"),
            "returns_dec": Decimal("0"),
        }
        for u in User.objects.filter(role=Role.REP, active=True).order_by("id")
    }
    lines: list[dict[str, Any]] = []

    allocs = (
        PaymentAllocation.objects.select_related(
            "payment",
            "invoice",
            "invoice__rep",
            "invoice__customer",
            "invoice__customer__rep",
        )
        .prefetch_related("invoice__lines__item")
        .filter(payment__reversed=False, payment__reversal_of__isnull=True, amount__gt=0)
        .order_by("-payment__created_at", "-id")
    )
    for alloc in allocs:
        day = baghdad_day_of(alloc.payment.created_at)
        if not day.startswith(month):
            continue
        inv = alloc.invoice
        rep = inv.rep or inv.customer.rep
        if not rep:
            continue
        rate = _invoice_rate(inv)
        bucket = by_rep.setdefault(
            rep.id,
            {
                "id": rep.id,
                "name": rep.name,
                "paid_sales": 0,
                "commission_dec": Decimal("0"),
                "returns_dec": Decimal("0"),
            },
        )
        bucket["paid_sales"] += alloc.amount
        bucket["commission_dec"] += Decimal(alloc.amount) * rate
        lines.append({
            "payment_id": alloc.payment_id,
            "payment_number": alloc.payment.number,
            "date": alloc.payment.created_at.isoformat(),
            "customer_id": inv.customer_id,
            "customer": inv.customer.name,
            "invoice_id": inv.id,
            "invoice_number": inv.number,
            "amount": alloc.amount,
            "rate": _fmt_rate_pct(rate),
            "commission": _round_iqd(Decimal(alloc.amount) * rate),
            "rep_id": rep.id,
        })

    rets = (
        ReturnDoc.objects.select_related("invoice", "rep", "customer", "customer__rep")
        .prefetch_related("invoice__lines__item")
        .filter(commission_base__gt=0)
        .order_by("created_at", "id")
    )
    for ret in rets:
        day = baghdad_day_of(ret.created_at)
        if not day.startswith(month):
            continue
        inv = ret.invoice
        rep = ret.rep or (inv.rep if inv else None) or ret.customer.rep
        if not rep or not inv:
            continue
        rate = _invoice_rate(inv)
        bucket = by_rep.setdefault(
            rep.id,
            {
                "id": rep.id,
                "name": rep.name,
                "paid_sales": 0,
                "commission_dec": Decimal("0"),
                "returns_dec": Decimal("0"),
            },
        )
        bucket["returns_dec"] += Decimal(ret.commission_base) * rate

    rows: list[dict[str, Any]] = []
    for rep_id, data in sorted(by_rep.items(), key=lambda kv: kv[0]):
        comm_int = _round_iqd(data["commission_dec"])
        ret_int = _round_iqd(data["returns_dec"])
        rows.append({
            "rep": {"id": rep_id, "name": data["name"]},
            "month": month,
            "paid_sales": data["paid_sales"],
            "commission": comm_int,
            "returns_deducted": ret_int,
            "net": comm_int - ret_int,
            "closed": False,
        })
    return rows, lines


def list_commissions(*, actor: User, month_raw: str | None = None) -> list[dict[str, Any]]:
    if actor.role not in (Role.MANAGER, Role.ACCOUNTANT, Role.REP):
        raise ApiException(code=ErrorCode.FORBIDDEN, status_code=http_status.HTTP_403_FORBIDDEN)

    month = _normalize_month(month_raw)
    closed_obj = ClosedCommissionMonth.objects.filter(month=month).first()
    if closed_obj:
        rows = [dict(r, closed=True) for r in (closed_obj.snapshot or [])]
    else:
        rows, _ = _compute_live_month(month)

    if actor.role == Role.REP:
        rows = [r for r in rows if (r.get("rep") or {}).get("id") == actor.id]
    return rows


def list_commission_lines(
    *, actor: User, month_raw: str | None = None, rep_id: int | None = None
) -> list[dict[str, Any]]:
    if actor.role not in (Role.MANAGER, Role.ACCOUNTANT, Role.REP):
        raise ApiException(code=ErrorCode.FORBIDDEN, status_code=http_status.HTTP_403_FORBIDDEN)

    month = _normalize_month(month_raw)
    _, lines = _compute_live_month(month)
    if actor.role == Role.REP:
        lines = [ln for ln in lines if ln.get("rep_id") == actor.id]
    elif rep_id is not None:
        lines = [ln for ln in lines if ln.get("rep_id") == rep_id]
    return lines


def list_closed_months(*, actor: User) -> list[str]:
    if actor.role not in (Role.MANAGER, Role.ACCOUNTANT, Role.REP):
        raise ApiException(code=ErrorCode.FORBIDDEN, status_code=http_status.HTTP_403_FORBIDDEN)
    return list(ClosedCommissionMonth.objects.order_by("-month").values_list("month", flat=True))


@transaction.atomic
def close_commission_month(*, actor: User, month_raw: str | None) -> list[dict[str, Any]]:
    if actor.role != Role.MANAGER:
        raise ApiException(code=ErrorCode.FORBIDDEN, status_code=http_status.HTTP_403_FORBIDDEN)

    month = _normalize_month(month_raw)
    if ClosedCommissionMonth.objects.filter(month=month).exists():
        raise ApiException(
            code=ErrorCode.MONTH_CLOSED,
            message=(f"شهر {month} مغلق مسبقاً", f"Month {month} is already closed"),
            status_code=http_status.HTTP_409_CONFLICT,
        )
    if month >= baghdad_today().strftime("%Y-%m"):
        raise ApiException(
            code=ErrorCode.BAD_STATE,
            message=("لا يمكن إغلاق الشهر قبل انتهائه", "You can't close a month before it ends"),
            status_code=http_status.HTTP_409_CONFLICT,
        )

    rows, _ = _compute_live_month(month)
    frozen_rows = [dict(r, closed=True) for r in rows]
    tot_coll = sum(int(r["paid_sales"]) for r in frozen_rows)
    tot_comm = sum(int(r["net"]) for r in frozen_rows)
    pct = SystemSetting.get_solo().commission_pct

    ClosedCommissionMonth.objects.create(
        month=month,
        pct=pct,
        snapshot=frozen_rows,
        total_collected=tot_coll,
        total_commission=tot_comm,
        closed_by=actor,
    )

    for r in frozen_rows:
        if r["net"] > 0:
            rep_id = r["rep"]["id"]
            notify_user(
                user_id=rep_id,
                kind="commission_earned",
                title_ar=f"عمولة شهر {month}: {r['net']:,} د.ع",
                title_en=f"Commission for {month}: {r['net']:,} IQD",
                body_ar=f"المبيعات المحصلة: {r['paid_sales']:,} د.ع",
                body_en=f"Paid sales: {r['paid_sales']:,} IQD",
                ref_type="commission",
                ref_id=month,
            )

    notify_role(
        role=Role.ACCOUNTANT,
        kind="commission_earned",
        title_ar=f"تم إغلاق عمولات شهر {month}",
        title_en=f"Commissions closed for {month}",
        body_ar=f"الإجمالي: {tot_comm:,} د.ع",
        body_en=f"Total commission: {tot_comm:,} IQD",
        ref_type="commission",
        ref_id=month,
    )
    record_audit(
        actor=actor,
        action="close",
        entity_type="commission",
        entity_id=month,
        summary=f"Closed commission month {month} (total {tot_comm:,} IQD)",
        summary_ar=f"إغلاق عمولات شهر {month} (الإجمالي {tot_comm:,} د.ع)",
        after={"month": month, "total_commission": tot_comm, "rows": len(frozen_rows)},
    )
    return frozen_rows
