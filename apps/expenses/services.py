"""Operating expense services: creation, manager approval, cancellation, and monthly totals."""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from rest_framework import status as http_status

from apps.accounts.health import DEFAULT_EXPENSE_APPROVAL_ABOVE
from apps.accounts.models import Role, SystemSetting, User
from apps.accounts.rules_engine import run_automation_rules
from apps.audit.services import record_audit
from apps.common.exceptions import ApiException, ErrorCode
from apps.common.utils import baghdad_today, decode_image_data_url
from apps.deliveries.models import Vehicle
from apps.expenses.models import Expense, ExpenseMethod, ExpenseStatus
from apps.notifications.services import notify_role, notify_user
from apps.warehouses.models import Warehouse
from integrations.storage import get_storage

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def serialize_expense(exp: Expense, *, request: Any = None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": exp.id,
        "number": exp.number,
        "date": exp.date.isoformat(),
        "category": exp.category,
        "amount": int(exp.amount),
        "method": exp.method,
        "paid_to": exp.paid_to,
        "status": exp.status,
        "created_by": {
            "id": exp.created_by_id,
            "name": exp.created_by.name,
            "role": exp.created_by.role,
        },
        "created_at": exp.created_at.isoformat(),
    }
    if exp.note:
        out["note"] = exp.note
    if exp.warehouse_id:
        out["warehouse_id"] = exp.warehouse_id
    if exp.vehicle_id:
        out["vehicle_id"] = exp.vehicle_id
    if exp.photo_path:
        out["photo"] = get_storage().get_url("delivery-proofs", exp.photo_path, request=request, private=True)
    if exp.decided_by:
        out["decided_by"] = {"id": exp.decided_by_id, "name": exp.decided_by.name}
    if exp.decided_at:
        out["decided_at"] = exp.decided_at.isoformat()
    if exp.reject_reason:
        out["reject_reason"] = exp.reject_reason
    if exp.cancelled_by:
        out["cancelled_by"] = {"id": exp.cancelled_by_id, "name": exp.cancelled_by.name}
    if exp.cancelled_at:
        out["cancelled_at"] = exp.cancelled_at.isoformat()
    if exp.cancel_reason:
        out["cancel_reason"] = exp.cancel_reason
    return out


def list_expenses(
    *,
    status_filter: str | None = None,
    category: str | None = None,
    warehouse_id: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    request: Any = None,
) -> list[dict[str, Any]]:
    qs = Expense.objects.select_related("created_by", "decided_by", "cancelled_by").order_by("-date", "-id")
    if status_filter:
        qs = qs.filter(status=status_filter)
    if category:
        qs = qs.filter(category=category)
    if warehouse_id:
        qs = qs.filter(warehouse_id=warehouse_id)
    if date_from and _DATE_RE.match(date_from):
        qs = qs.filter(date__gte=date_from)
    if date_to and _DATE_RE.match(date_to):
        qs = qs.filter(date__lte=date_to)
    return [serialize_expense(e, request=request) for e in qs]


def get_expense(*, expense_id: int, request: Any = None) -> dict[str, Any]:
    exp = (
        Expense.objects.select_related("created_by", "decided_by", "cancelled_by")
        .filter(id=expense_id)
        .first()
    )
    if not exp:
        raise ApiException(code=ErrorCode.NOT_FOUND, status_code=http_status.HTTP_404_NOT_FOUND)
    return serialize_expense(exp, request=request)


@transaction.atomic
def create_expense(*, actor: User, data: dict[str, Any], request: Any = None) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ApiException(code=ErrorCode.VALIDATION, message=("البيانات غير صالحة", "Invalid payload"))

    errors: dict[str, tuple[str, str]] = {}
    amount = 0
    try:
        amount = round(float(data.get("amount") or 0))
        if amount <= 0:
            errors["amount"] = ("اكتب المبلغ", "Enter the amount")
        elif amount > 10_000_000_000:
            errors["amount"] = ("المبلغ كبير جداً", "The amount is too big")
    except (TypeError, ValueError):
        errors["amount"] = ("اكتب المبلغ", "Enter the amount")

    category = str(data.get("category") or "").strip()[:40]
    if not category:
        errors["category"] = ("اختر البند", "Choose the category")

    paid_to = str(data.get("paid_to") or "").strip()[:180]
    if len(paid_to) < 2:
        errors["paid_to"] = ("لمن دُفع المبلغ؟", "Who got the money?")

    raw_date = str(data.get("date") or "").strip()
    today = baghdad_today()
    exp_date: date | None = None
    if not _DATE_RE.match(raw_date):
        errors["date"] = ("اختر التاريخ", "Choose the date")
    else:
        try:
            exp_date = datetime.strptime(raw_date, "%Y-%m-%d").date()
            if exp_date > today:
                errors["date"] = ("لا يُسجَّل صرف بتاريخ قادم", "An expense can't be in the future")
        except ValueError:
            errors["date"] = ("تاريخ غير صحيح", "Invalid date")

    method = str(data.get("method") or "").strip().lower()
    if method not in (ExpenseMethod.CASH, ExpenseMethod.TRANSFER):
        errors["method"] = ("اختر طريقة الدفع", "Choose how it was paid")

    wh_id = data.get("warehouse_id") or None
    wh = None
    if wh_id:
        wh = Warehouse.objects.filter(id=wh_id).first()
        if not wh:
            errors["warehouse_id"] = ("مخزن غير موجود", "No such warehouse")

    veh_id = data.get("vehicle_id") or None
    veh = None
    if veh_id:
        veh = Vehicle.objects.filter(id=veh_id).first()
        if not veh:
            errors["vehicle_id"] = ("سيارة غير موجودة", "No such vehicle")

    photo_raw = data.get("photo")
    photo_bytes: tuple[bytes, str, str] | None = None
    if photo_raw:
        if len(str(photo_raw)) > 700_000:
            errors["photo"] = ("صورة غير صالحة أو كبيرة جداً", "Not a valid picture, or too big")
        else:
            try:
                photo_bytes = decode_image_data_url(str(photo_raw), max_bytes=500 * 1024, max_dim=1400)
            except ApiException:
                errors["photo"] = ("صورة غير صالحة أو كبيرة جداً", "Not a valid picture, or too big")

    if errors:
        raise ApiException(
            code=ErrorCode.VALIDATION,
            message=("تحقق من الحقول", "Check the fields"),
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            errors=errors,
        )

    assert exp_date is not None
    setting = SystemSetting.get_solo()
    limit = (
        setting.expense_approval_above
        if setting.expense_approval_above is not None
        else DEFAULT_EXPENSE_APPROVAL_ABOVE
    )
    waits_limit = actor.role != Role.MANAGER and amount > limit

    exp = Expense.objects.create(
        number=f"EXP-TMP-{timezone.now().timestamp()}",
        date=exp_date,
        category=category,
        amount=int(amount),
        method=method,
        paid_to=paid_to,
        note=str(data.get("note") or "").strip(),
        warehouse=wh,
        vehicle=veh,
        status=ExpenseStatus.PENDING if waits_limit else ExpenseStatus.APPROVED,
        created_by=actor,
    )
    exp.number = f"EXP-{exp.id:04d}"
    update_fields = ["number"]

    if photo_bytes is not None:
        raw_img, ext, mime = photo_bytes
        key = get_storage().put_bytes("delivery-proofs", f"expenses/{exp.id}.{ext}", raw_img, mime)
        exp.photo_path = key
        update_fields.append("photo_path")

    rule_approvals = run_automation_rules(
        "expense_created",
        {"amount": int(amount), "category": category, "method": method},
        number=exp.number,
        link=f"/expenses/{exp.id}",
        by=actor.name,
    )
    if rule_approvals and actor.role != Role.MANAGER and exp.status != ExpenseStatus.PENDING:
        exp.status = ExpenseStatus.PENDING
        update_fields.append("status")

    exp.save(update_fields=update_fields)

    record_audit(
        actor=actor,
        action="create",
        entity_type="expense",
        entity_id=exp.id,
        summary=f"Expense {exp.number}: {amount:,} IQD ({category})",
        after={"amount": exp.amount, "category": exp.category, "status": exp.status},
    )
    if exp.status == ExpenseStatus.PENDING:
        notify_role(
            role=Role.MANAGER,
            kind="expense",
            title_ar=f"صرف يحتاج موافقتك: {amount:,} د.ع",
            title_en=f"An expense needs your OK: {amount:,} IQD",
            body_ar=f"{exp.number} · {category} · إلى {paid_to} · {actor.name}",
            body_en=f"{exp.number} · {actor.name}",
            ref_type="expense",
            ref_id=exp.id,
        )

    return serialize_expense(exp, request=request)


@transaction.atomic
def decide_expense(
    *, actor: User, expense_id: int, ok: bool, reason: str = "", request: Any = None
) -> dict[str, Any]:
    exp = Expense.objects.select_for_update().select_related("created_by").filter(id=expense_id).first()
    if not exp:
        raise ApiException(code=ErrorCode.NOT_FOUND, status_code=http_status.HTTP_404_NOT_FOUND)
    if exp.status != ExpenseStatus.PENDING:
        raise ApiException(
            code=ErrorCode.BAD_STATE,
            message=("هذا السند لا ينتظر موافقة", "This expense isn't waiting"),
            status_code=http_status.HTTP_409_CONFLICT,
        )

    clean_reason = str(reason or "").strip()
    if not ok and len(clean_reason) < 3:
        msg = ("اكتب سبب الرفض", "Write why")
        raise ApiException(
            code=ErrorCode.VALIDATION,
            message=msg,
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            errors={"reason": msg},
        )

    exp.status = ExpenseStatus.APPROVED if ok else ExpenseStatus.REJECTED
    exp.decided_by = actor
    exp.decided_at = timezone.now()
    if not ok:
        exp.reject_reason = clean_reason
    exp.save(update_fields=["status", "decided_by", "decided_at", "reject_reason", "updated_at"])

    record_audit(
        actor=actor,
        action="approve" if ok else "cancel",
        entity_type="expense",
        entity_id=exp.id,
        summary=f"{'Approved' if ok else 'Rejected'} expense {exp.number}",
    )
    notify_user(
        user_id=exp.created_by_id,
        kind="expense",
        title_ar=f"وافق المدير على الصرف {exp.number}" if ok else f"رفض المدير الصرف {exp.number}",
        title_en=f"{exp.number} approved" if ok else f"{exp.number} rejected",
        body_ar=f"{exp.amount:,} د.ع · {exp.category}" if ok else clean_reason,
        body_en=f"{exp.amount} IQD" if ok else clean_reason,
        ref_type="expense",
        ref_id=exp.id,
    )
    return serialize_expense(exp, request=request)


@transaction.atomic
def cancel_expense(*, actor: User, expense_id: int, reason: str, request: Any = None) -> dict[str, Any]:
    exp = Expense.objects.select_for_update().select_related("created_by", "decided_by").filter(id=expense_id).first()
    if not exp:
        raise ApiException(code=ErrorCode.NOT_FOUND, status_code=http_status.HTTP_404_NOT_FOUND)
    if exp.status in (ExpenseStatus.CANCELLED, ExpenseStatus.REJECTED):
        raise ApiException(
            code=ErrorCode.BAD_STATE,
            message=("السند ملغي أو مرفوض أصلاً", "Already cancelled or rejected"),
            status_code=http_status.HTTP_409_CONFLICT,
        )

    clean_reason = str(reason or "").strip()
    if len(clean_reason) < 3:
        msg = ("اكتب سبب الإلغاء", "Write why")
        raise ApiException(
            code=ErrorCode.VALIDATION,
            message=msg,
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            errors={"reason": msg},
        )

    if actor.role != Role.MANAGER and exp.decided_by_id is not None:
        raise ApiException(
            code=ErrorCode.FORBIDDEN,
            message=("وافق عليه المدير: إلغاؤه من المدير فقط", "The manager approved it: only he can cancel it"),
            status_code=http_status.HTTP_403_FORBIDDEN,
        )

    exp.status = ExpenseStatus.CANCELLED
    exp.cancelled_by = actor
    exp.cancelled_at = timezone.now()
    exp.cancel_reason = clean_reason
    exp.save(update_fields=["status", "cancelled_by", "cancelled_at", "cancel_reason", "updated_at"])

    record_audit(
        actor=actor,
        action="cancel",
        entity_type="expense",
        entity_id=exp.id,
        summary=f"Cancelled expense {exp.number}: {clean_reason}",
    )
    return serialize_expense(exp, request=request)


def month_expenses_total(month_prefix: str, *, warehouse_id: int | None = None) -> int:
    qs = Expense.objects.filter(
        status=ExpenseStatus.APPROVED,
        date__startswith=month_prefix,
    )
    if warehouse_id is not None:
        qs = qs.filter(warehouse_id=warehouse_id)
    return int(qs.aggregate(total=Sum("amount"))["total"] or 0)
