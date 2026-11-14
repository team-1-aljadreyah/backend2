"""Payment allocation, reversal, refund, customer statement, and Qi Card settlement services."""
from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from django.apps import apps
from django.db import transaction
from django.db.models import Q, Sum
from rest_framework import status as http_status

from apps.accounts.business_rules import rules_of
from apps.accounts.models import Role, User
from apps.accounts.permissions import get_active_customer_id
from apps.accounts.profiles import may_profile_action
from apps.accounts.rules_engine import run_automation_rules
from apps.audit.services import record_audit
from apps.common.exceptions import ApiException, ErrorCode
from apps.common.utils import baghdad_day_of, baghdad_now
from apps.customers.models import Customer
from apps.customers.services import customer_balance_map, serialize_customer
from apps.invoices.models import CreditNote, Invoice, ReturnDoc
from apps.notifications.services import notify_customer, notify_role
from apps.payments.models import (
    OnlinePayment,
    OnlinePaymentStatus,
    Payment,
    PaymentAllocation,
    PaymentMethod,
    Refund,
)
from integrations.qi_card.client import QiCardGateway

logger = logging.getLogger(__name__)


def _can_see_customer_finance(actor: User, customer: Customer) -> bool:
    if actor.role in (Role.MANAGER, Role.ACCOUNTANT):
        return True
    if actor.role == Role.REP:
        if customer.rep_id == actor.id:
            return True
        return Invoice.objects.filter(customer=customer, delivery_rep=actor).exists()
    if actor.role == Role.SHOP:
        return get_active_customer_id(actor) == customer.id
    return False


def serialize_payment(p: Payment) -> dict[str, Any]:
    rev_entry = getattr(p, "reversed_by_entry", None)
    return {
        "id": p.id,
        "number": p.number,
        "client_ref": p.client_ref or None,
        "date": p.created_at.isoformat(),
        "customer_id": p.customer_id,
        "amount": p.amount,
        "unallocated_amount": p.unallocated_amount,
        "method": p.method,
        "note": p.note,
        "reason": p.reason,
        "reversed": bool(p.reversed),
        "reversal_of": p.reversal_of_id,
        "reversed_by_id": rev_entry.id if rev_entry else None,
        "received_by": {
            "id": p.received_by_id or 0,
            "name": p.received_by.name if p.received_by else (p.received_by_name or "Qi Card"),
            "role": p.received_by.role if p.received_by else (p.received_by_role or Role.SHOP),
        },
        "gateway": p.gateway,
        "allocations": [
            {
                "invoice_id": a.invoice_id,
                "number": a.invoice.number,
                "amount": a.amount,
            }
            for a in p.allocations.select_related("invoice").order_by("id")
        ],
    }


def serialize_refund(ref: Refund) -> dict[str, Any]:
    actor_info = {
        "id": ref.created_by_id,
        "name": ref.created_by.name,
        "role": ref.created_by.role,
    }
    at_iso = ref.created_at.isoformat()
    return {
        "id": ref.id,
        "number": ref.number,
        "customer_id": ref.customer_id,
        "payment_id": ref.payment_id,
        "amount": ref.amount,
        "method": ref.method,
        "reason": ref.reason,
        "by": actor_info,
        "at": at_iso,
        "created_by": actor_info,
        "created_at": at_iso,
    }


def serialize_online_payment(op: OnlinePayment) -> dict[str, Any]:
    by_user = getattr(op, "initiated_by", None)
    by_payload = (
        {"id": by_user.id, "name": by_user.name, "role": by_user.role}
        if by_user is not None
        else {"id": 0, "name": op.customer.name if op.customer_id else "", "role": Role.SHOP}
    )
    return {
        "payment_id": op.payment_id,
        "provider": op.provider or "qi",
        "gateway_id": op.gateway_id or None,
        "customer_id": op.customer_id,
        "amount": op.amount,
        "status": op.status,
        "form_url": op.form_url,
        "return_url": op.return_url,
        "recorded_payment_id": op.recorded_payment_id,
        "note": op.note or None,
        "by": by_payload,
        "created_at": op.created_at.isoformat(),
    }


def list_payments_for_actor(*, actor: User) -> list[dict[str, Any]]:
    if actor.role in (Role.WAREHOUSE, Role.STAFF):
        raise ApiException(code=ErrorCode.FORBIDDEN, status_code=http_status.HTTP_403_FORBIDDEN)
    qs = (
        Payment.objects.select_related("received_by", "reversed_by_entry")
        .prefetch_related("allocations__invoice")
        .order_by("-id")
    )
    if actor.role == Role.REP:
        cust_ids = list(
            Customer.objects.filter(Q(rep_id=actor.id) | Q(invoices__delivery_rep_id=actor.id))
            .values_list("id", flat=True)
            .distinct()
        )
        qs = qs.filter(customer_id__in=cust_ids)
    elif actor.role == Role.SHOP:
        cid = get_active_customer_id(actor)
        qs = qs.filter(customer_id=cid) if cid else qs.none()
    return [serialize_payment(p) for p in qs]


def get_payment_for_actor(*, actor: User, payment_id: int) -> dict[str, Any]:
    if actor.role in (Role.WAREHOUSE, Role.STAFF):
        raise ApiException(code=ErrorCode.FORBIDDEN, status_code=http_status.HTTP_403_FORBIDDEN)
    p = (
        Payment.objects.select_related("customer", "received_by", "reversed_by_entry")
        .prefetch_related("allocations__invoice")
        .filter(id=payment_id)
        .first()
    )
    if not p:
        raise ApiException(code=ErrorCode.NOT_FOUND, status_code=http_status.HTTP_404_NOT_FOUND)
    if not _can_see_customer_finance(actor, p.customer):
        raise ApiException(code=ErrorCode.FORBIDDEN, status_code=http_status.HTTP_403_FORBIDDEN)
    return serialize_payment(p)


def preview_allocation(*, actor: User, customer_id: int, amount: int) -> list[dict[str, Any]]:
    customer = Customer.objects.filter(id=customer_id).first()
    if not customer:
        raise ApiException(code=ErrorCode.NOT_FOUND, status_code=http_status.HTTP_404_NOT_FOUND)
    if not _can_see_customer_finance(actor, customer):
        raise ApiException(code=ErrorCode.FORBIDDEN, status_code=http_status.HTTP_403_FORBIDDEN)
    if amount <= 0:
        return []

    left = amount
    rows: list[dict[str, Any]] = []
    for inv in Invoice.objects.filter(customer_id=customer_id).order_by("delivered_at", "id"):
        if left <= 0:
            break
        rem = inv.remaining
        if rem <= 0:
            continue
        take = min(rem, left)
        rows.append({"invoice_id": inv.id, "number": inv.number, "amount": take})
        left -= take
    return rows


def _allocate_payment_fifo(
    *,
    customer: Customer,
    amount: int,
    method: str,
    note: str,
    received_by: User | None,
    received_by_name: str,
    received_by_role: str,
    client_ref: str | None = None,
    gateway: dict[str, Any] | None = None,
) -> Payment:
    """Create a Payment and allocate it FIFO across customer's unpaid invoices (inside @transaction.atomic)."""
    pay = Payment.objects.create(
        number="PAY-TMP",
        customer=customer,
        received_by=received_by,
        received_by_name=received_by_name,
        received_by_role=received_by_role,
        method=method,
        amount=amount,
        unallocated_amount=0,
        note=note,
        client_ref=client_ref,
        gateway=gateway,
    )
    pay.number = f"PAY-{pay.id:05d}"

    left = amount
    for inv in Invoice.objects.select_for_update().filter(customer=customer).order_by("delivered_at", "id"):
        if left <= 0:
            break
        rem = inv.remaining
        if rem <= 0:
            continue
        take = min(rem, left)
        inv.paid += take
        inv.save(update_fields=["paid"])
        PaymentAllocation.objects.create(payment=pay, invoice=inv, amount=take)
        left -= take

    pay.unallocated_amount = left
    pay.save(update_fields=["number", "unallocated_amount"])

    notify_customer(
        customer_id=customer.id,
        kind="payment",
        title_ar=f"استلام تسديد {pay.number}",
        title_en=f"Payment received {pay.number}",
        body_ar=f"تم تسجيل مبلغ {amount:,} د.ع",
        body_en=f"Recorded {amount:,} IQD",
        ref_type="payment",
        ref_id=pay.id,
    )
    record_audit(
        actor=received_by,
        action="create",
        entity_type="payment",
        entity_id=pay.id,
        summary=f"Recorded payment {pay.number} ({amount:,} IQD, {method}) for {customer.name}",
        summary_ar=f"تسجيل تسديد {pay.number} بمبلغ {amount:,} د.ع من {customer.name}",
        after={"customer_id": customer.id, "amount": amount, "method": method, "unallocated": left},
    )
    return (
        Payment.objects.select_related("received_by", "reversed_by_entry")
        .prefetch_related("allocations__invoice")
        .get(id=pay.id)
    )


def apply_available_credit_to_open_invoices(*, customer_id: int) -> None:
    """
    Allocate unrefunded advance payment credits FIFO to open customer invoices.
    Caller must already hold an @transaction.atomic block.
    """
    invs = list(
        Invoice.objects.select_for_update()
        .filter(customer_id=customer_id)
        .order_by("delivered_at", "id")
    )
    open_invs = [inv for inv in invs if inv.remaining > 0]
    if not open_invs:
        return

    pays = list(
        Payment.objects.select_for_update()
        .filter(
            customer_id=customer_id,
            reversed=False,
            reversal_of__isnull=True,
            unallocated_amount__gt=0,
        )
        .order_by("created_at", "id")
    )
    unalloc_total = sum(p.unallocated_amount for p in pays)
    if unalloc_total <= 0:
        return

    total_refunds = int(
        Refund.objects.filter(customer_id=customer_id).aggregate(s=Sum("amount"))["s"] or 0
    )
    over_credit_from_invoices = sum(max(0, -inv.remaining) for inv in invs)
    # Subtract refunds not already backed by over-returned/credited invoices so refunded cash is never reused.
    refunds_consuming_unalloc = max(0, total_refunds - over_credit_from_invoices)
    pool = max(0, unalloc_total - refunds_consuming_unalloc)
    if pool <= 0:
        return

    for pay in pays:
        if pool <= 0:
            break
        avail = min(pay.unallocated_amount, pool)
        used = 0
        for inv in open_invs:
            if avail <= 0:
                break
            rem = inv.remaining
            if rem <= 0:
                continue
            take = min(rem, avail)
            inv.paid += take
            inv.save(update_fields=["paid"])
            PaymentAllocation.objects.create(payment=pay, invoice=inv, amount=take)
            avail -= take
            used += take
        if used > 0:
            pay.unallocated_amount -= used
            pay.save(update_fields=["unallocated_amount"])
            pool -= used


@transaction.atomic
def create_payment(*, actor: User, data: dict[str, Any]) -> dict[str, Any]:
    if actor.role not in (Role.MANAGER, Role.ACCOUNTANT, Role.REP):
        raise ApiException(code=ErrorCode.FORBIDDEN, status_code=http_status.HTTP_403_FORBIDDEN)
    may_profile_action(actor, "payments")

    client_ref = (str(data.get("client_ref") or "")).strip() or None
    if client_ref:
        existing = (
            Payment.objects.select_related("received_by", "reversed_by_entry")
            .prefetch_related("allocations__invoice")
            .filter(client_ref=client_ref)
            .first()
        )
        if existing:
            return serialize_payment(existing)

    customer = Customer.objects.select_for_update().filter(id=data.get("customer_id")).first()
    if not customer:
        raise ApiException(code=ErrorCode.NOT_FOUND, message=("الزبون غير موجود", "Customer not found"), status_code=http_status.HTTP_404_NOT_FOUND)
    if actor.role == Role.REP and not _can_see_customer_finance(actor, customer):
        raise ApiException(code=ErrorCode.FORBIDDEN, status_code=http_status.HTTP_403_FORBIDDEN)

    try:
        amount = int(data.get("amount"))
    except (TypeError, ValueError) as exc:
        msg = ("أدخل مبلغاً صحيحاً", "Enter a valid amount")
        raise ApiException(code=ErrorCode.VALIDATION, message=msg, errors={"amount": msg}) from exc
    if amount <= 0:
        msg = ("المبلغ يجب أن يكون أكبر من صفر", "Amount must be positive")
        raise ApiException(code=ErrorCode.VALIDATION, message=msg, errors={"amount": msg})

    method = str(data.get("method") or PaymentMethod.CASH).strip().lower()
    if method not in (PaymentMethod.CASH, PaymentMethod.TRANSFER):
        msg = ("طريقة الدفع غير صالحة", "Invalid payment method")
        raise ApiException(code=ErrorCode.VALIDATION, message=msg, errors={"method": msg})

    note = (str(data.get("note") or "")).strip()
    pay = _allocate_payment_fifo(
        customer=customer,
        amount=amount,
        method=method,
        note=note,
        received_by=actor,
        received_by_name=actor.name,
        received_by_role=actor.role,
        client_ref=client_ref,
    )
    run_automation_rules(
        "payment_created",
        {"amount": amount, "method": method},
        number=pay.number,
        link="/payments",
        by=actor.name,
        customer=customer.name,
    )
    if actor.role == Role.REP:
        from apps.customers.visits_services import record_auto_visit

        record_auto_visit(actor=actor, customer=customer, outcome="payment")
        if method == PaymentMethod.CASH:
            from apps.payments.custody_services import check_rep_custody_cap

            check_rep_custody_cap(actor)
    return serialize_payment(pay)


@transaction.atomic
def reverse_payment(*, actor: User, payment_id: int, reason: str) -> dict[str, Any]:
    rules = rules_of()
    allowed_roles = rules.get("reverse_roles") or [Role.MANAGER, Role.ACCOUNTANT]
    if actor.role not in allowed_roles:
        raise ApiException(code=ErrorCode.FORBIDDEN, status_code=http_status.HTTP_403_FORBIDDEN)

    clean_reason = (reason or "").strip()
    if not clean_reason:
        msg = ("سبب العكس مطلوب", "Reversal reason is required")
        raise ApiException(code=ErrorCode.VALIDATION, message=msg, errors={"reason": msg})

    orig = (
        Payment.objects.select_for_update()
        .select_related("customer", "received_by")
        .filter(id=payment_id)
        .first()
    )
    if not orig:
        raise ApiException(code=ErrorCode.NOT_FOUND, status_code=http_status.HTTP_404_NOT_FOUND)
    if orig.reversed or orig.reversal_of_id is not None:
        raise ApiException(
            code=ErrorCode.ALREADY_REVERSED,
            message=("هذا التسديد معكوس مسبقاً", "This payment is already reversed"),
        )

    max_days = int(rules.get("reverse_days") or 0)
    if max_days > 0 and actor.role != Role.MANAGER:
        age_seconds = (baghdad_now() - orig.created_at).total_seconds()
        if age_seconds > max_days * 86400:
            raise ApiException(
                code=ErrorCode.TOO_OLD,
                message=(
                    f"عكس التسديد للأكبر من {max_days} يوماً للمدير فقط",
                    f"Reversing payments older than {max_days} days is manager-only",
                ),
                status_code=http_status.HTTP_403_FORBIDDEN,
            )

    month_str = baghdad_day_of(orig.created_at)[:7]
    if apps.is_installed("apps.commissions"):
        from apps.commissions.models import ClosedCommissionMonth

        if ClosedCommissionMonth.objects.filter(month=month_str).exists():
            raise ApiException(
                code=ErrorCode.MONTH_CLOSED,
                message=(f"شهر {month_str} مغلق ولا يمكن عكس تسديداته", f"Month {month_str} is closed for reversals"),
                status_code=http_status.HTTP_409_CONFLICT,
            )

    now = datetime.now(tz=UTC)
    rev_entry = Payment.objects.create(
        number="REV-TMP",
        customer_id=orig.customer_id,
        received_by=actor,
        received_by_name=actor.name,
        received_by_role=actor.role,
        method=orig.method,
        amount=-orig.amount,
        unallocated_amount=-orig.unallocated_amount,
        note=orig.note,
        reason=clean_reason,
        reversal_of=orig,
    )
    rev_entry.number = f"REV-{rev_entry.id:05d}"
    rev_entry.save(update_fields=["number"])

    for alloc in orig.allocations.order_by("invoice_id", "id"):
        inv = Invoice.objects.select_for_update().get(id=alloc.invoice_id)
        inv.paid = max(0, inv.paid - alloc.amount)
        inv.save(update_fields=["paid"])
        PaymentAllocation.objects.create(payment=rev_entry, invoice=inv, amount=-alloc.amount)

    orig.reversed = True
    orig.reversed_by = actor
    orig.reversed_at = now
    orig.reason = clean_reason
    orig.save(update_fields=["reversed", "reversed_by", "reversed_at", "reason"])

    apply_available_credit_to_open_invoices(customer_id=orig.customer_id)

    notify_role(
        role=Role.MANAGER,
        kind="payment_reversed",
        title_ar=f"تم عكس التسديد {orig.number}",
        title_en=f"Payment {orig.number} reversed",
        body_ar=clean_reason,
        body_en=clean_reason,
        ref_type="payment",
        ref_id=orig.id,
    )
    record_audit(
        actor=actor,
        action="reverse",
        entity_type="payment",
        entity_id=orig.id,
        summary=f"Reversed payment {orig.number} ({orig.amount:,} IQD): {clean_reason}",
        summary_ar=f"عكس التسديد {orig.number} بمبلغ {orig.amount:,} د.ع: {clean_reason}",
        after={"reversal_id": rev_entry.id, "reason": clean_reason},
    )

    refreshed = (
        Payment.objects.select_related("received_by", "reversed_by_entry")
        .prefetch_related("allocations__invoice")
        .get(id=orig.id)
    )
    return serialize_payment(refreshed)


def list_credit_notes_for_customer(*, actor: User, customer_id: int) -> list[dict[str, Any]]:
    customer = Customer.objects.filter(id=customer_id).first()
    if not customer:
        raise ApiException(code=ErrorCode.NOT_FOUND, status_code=http_status.HTTP_404_NOT_FOUND)
    if not _can_see_customer_finance(actor, customer):
        raise ApiException(code=ErrorCode.FORBIDDEN, status_code=http_status.HTTP_403_FORBIDDEN)
    from apps.invoices.services import serialize_credit_note

    return [
        serialize_credit_note(cn)
        for cn in CreditNote.objects.select_related("created_by").filter(customer_id=customer_id).order_by("-id")
    ]


def list_refunds_for_customer(*, actor: User, customer_id: int) -> list[dict[str, Any]]:
    customer = Customer.objects.filter(id=customer_id).first()
    if not customer:
        raise ApiException(code=ErrorCode.NOT_FOUND, status_code=http_status.HTTP_404_NOT_FOUND)
    if not _can_see_customer_finance(actor, customer):
        raise ApiException(code=ErrorCode.FORBIDDEN, status_code=http_status.HTTP_403_FORBIDDEN)
    return [serialize_refund(r) for r in Refund.objects.select_related("created_by").filter(customer_id=customer_id).order_by("-id")]


@transaction.atomic
def create_refund(*, actor: User, customer_id: int, data: dict[str, Any]) -> dict[str, Any]:
    if actor.role not in (Role.MANAGER, Role.ACCOUNTANT):
        raise ApiException(code=ErrorCode.FORBIDDEN, status_code=http_status.HTTP_403_FORBIDDEN)

    customer = Customer.objects.select_for_update().filter(id=customer_id).first()
    if not customer:
        raise ApiException(code=ErrorCode.NOT_FOUND, status_code=http_status.HTTP_404_NOT_FOUND)

    try:
        amount = int(data.get("amount"))
    except (TypeError, ValueError) as exc:
        msg = ("أدخل مبلغاً صحيحاً", "Enter a valid amount")
        raise ApiException(code=ErrorCode.VALIDATION, message=msg, errors={"amount": msg}) from exc

    if amount <= 0:
        msg = ("المبلغ يجب أن يكون أكبر من صفر", "Amount must be positive")
        raise ApiException(code=ErrorCode.VALIDATION, message=msg, errors={"amount": msg})

    cur_balance = customer_balance_map([customer.id]).get(customer.id, 0)
    available_credit = max(0, -cur_balance)
    if available_credit <= 0 or amount > available_credit:
        msg = (
            f"لا يوجد رصيد زائد كافٍ لإعادته (المتاح {available_credit:,} د.ع)",
            f"Insufficient overpaid credit for refund (available {available_credit:,} IQD)",
        )
        raise ApiException(code=ErrorCode.VALIDATION, message=msg, errors={"amount": msg})

    reason = (str(data.get("reason") or "")).strip()
    if not reason:
        msg = ("السبب مطلوب", "Reason is required")
        raise ApiException(code=ErrorCode.VALIDATION, message=msg, errors={"reason": msg})

    method = str(data.get("method") or PaymentMethod.CASH).strip().lower()
    if method not in (PaymentMethod.CASH, PaymentMethod.TRANSFER, PaymentMethod.ONLINE):
        msg = ("طريقة الإرجاع غير صالحة", "Invalid refund method")
        raise ApiException(code=ErrorCode.VALIDATION, message=msg, errors={"method": msg})

    pay_id = data.get("payment_id")
    pay = Payment.objects.filter(id=pay_id, customer_id=customer_id).first() if pay_id else None

    ref = Refund.objects.create(
        number="REF-TMP",
        customer=customer,
        payment=pay,
        amount=amount,
        method=method,
        reason=reason,
        created_by=actor,
    )
    ref.number = f"REF-{ref.id:05d}"
    ref.save(update_fields=["number"])

    notify_customer(
        customer_id=customer.id,
        kind="refund",
        title_ar=f"إرجاع مبلغ {ref.number}",
        title_en=f"Refund issued {ref.number}",
        body_ar=f"تمت إعادة {amount:,} د.ع: {reason}",
        body_en=f"Refunded {amount:,} IQD: {reason}",
        ref_type="refund",
        ref_id=ref.id,
    )
    record_audit(
        actor=actor,
        action="create",
        entity_type="customer",
        entity_id=customer.id,
        summary=f"Issued refund {ref.number} ({amount:,} IQD) to {customer.name}",
        summary_ar=f"إعادة مبلغ {amount:,} د.ع للزبون {customer.name} ({ref.number})",
        after={"refund_id": ref.id, "amount": amount, "method": method, "reason": reason},
    )
    return serialize_refund(ref)


def get_customer_statement(
    *, actor: User, customer_id: int, date_from: str | None = None, date_to: str | None = None
) -> dict[str, Any]:
    customer = Customer.objects.filter(id=customer_id).first()
    if not customer:
        raise ApiException(code=ErrorCode.NOT_FOUND, status_code=http_status.HTTP_404_NOT_FOUND)
    if not _can_see_customer_finance(actor, customer):
        raise ApiException(code=ErrorCode.FORBIDDEN, status_code=http_status.HTTP_403_FORBIDDEN)

    events: list[tuple[datetime, int, int, dict[str, Any]]] = []

    for inv in Invoice.objects.filter(customer_id=customer_id).only("id", "number", "total", "delivered_at"):
        day = baghdad_day_of(inv.delivered_at)
        events.append((
            inv.delivered_at,
            1,
            inv.id,
            {"date": day, "at": inv.delivered_at.isoformat(), "type": "invoice", "ref": inv.number, "link": f"/invoices/{inv.id}", "debit": inv.total, "credit": 0},
        ))

    for p in Payment.objects.filter(customer_id=customer_id).only("id", "number", "amount", "reversal_of_id", "created_at"):
        day = baghdad_day_of(p.created_at)
        pay_link = None if actor.role == Role.SHOP else f"/payments/{p.id}"
        if p.reversal_of_id is not None or p.amount < 0:
            events.append((
                p.created_at,
                2,
                p.id,
                {"date": day, "at": p.created_at.isoformat(), "type": "reversal", "ref": p.number, "link": pay_link, "debit": abs(p.amount), "credit": 0},
            ))
        else:
            events.append((
                p.created_at,
                2,
                p.id,
                {"date": day, "at": p.created_at.isoformat(), "type": "payment", "ref": p.number, "link": pay_link, "debit": 0, "credit": p.amount},
            ))

    for ret in ReturnDoc.objects.filter(customer_id=customer_id).only("id", "number", "amount", "created_at"):
        day = baghdad_day_of(ret.created_at)
        events.append((
            ret.created_at,
            3,
            ret.id,
            {"date": day, "at": ret.created_at.isoformat(), "type": "return", "ref": ret.number, "link": f"/returns/{ret.id}", "debit": 0, "credit": ret.amount},
        ))

    for cn in CreditNote.objects.filter(customer_id=customer_id).only("id", "number", "invoice_id", "amount", "created_at"):
        day = baghdad_day_of(cn.created_at)
        events.append((
            cn.created_at,
            4,
            cn.id,
            {"date": day, "at": cn.created_at.isoformat(), "type": "credit_note", "ref": cn.number, "link": f"/invoices/{cn.invoice_id}", "debit": 0, "credit": cn.amount},
        ))

    for rf in Refund.objects.filter(customer_id=customer_id).only("id", "number", "amount", "created_at"):
        day = baghdad_day_of(rf.created_at)
        events.append((
            rf.created_at,
            5,
            rf.id,
            {"date": day, "at": rf.created_at.isoformat(), "type": "refund", "ref": rf.number, "link": None, "debit": rf.amount, "credit": 0},
        ))

    events.sort(key=lambda e: (e[0], e[1], e[2]))

    opening_bal = 0
    rows: list[dict[str, Any]] = []
    running = 0
    total_debit = 0
    total_credit = 0

    for _, _, _, row in events:
        d = row["date"]
        if date_from and d < date_from:
            opening_bal += row["debit"] - row["credit"]
            continue
        if date_to and d > date_to:
            continue
        if date_from and not rows:
            running = opening_bal
            rows.append({
                "date": date_from,
                "type": "opening",
                "ref": "",
                "link": None,
                "debit": opening_bal if opening_bal > 0 else 0,
                "credit": -opening_bal if opening_bal < 0 else 0,
                "balance": running,
            })
            if opening_bal > 0:
                total_debit += opening_bal
            else:
                total_credit += -opening_bal

        running += row["debit"] - row["credit"]
        total_debit += row["debit"]
        total_credit += row["credit"]
        rows.append({
            "date": row["date"],
            "type": row["type"],
            "ref": row["ref"],
            "link": row["link"],
            "debit": row["debit"],
            "credit": row["credit"],
            "balance": running,
        })

    if date_from and not rows:
        running = opening_bal
        rows.append({
            "date": date_from,
            "type": "opening",
            "ref": "",
            "link": None,
            "debit": opening_bal if opening_bal > 0 else 0,
            "credit": -opening_bal if opening_bal < 0 else 0,
            "balance": running,
        })
        if opening_bal > 0:
            total_debit += opening_bal
        else:
            total_credit += -opening_bal

    return {
        "customer_id": customer_id,
        "customer": serialize_customer(customer, actor=actor),
        "rows": rows,
        "total_debit": total_debit,
        "total_credit": total_credit,
        "balance": running if rows else opening_bal,
    }


def _resolve_online_return_url(return_url: str, *, payment_key: str, via: str) -> str:
    """Resolve relative UI path into an absolute return URL carrying `?payment=...`."""
    raw = str(return_url or "/statement").strip()
    if raw.startswith("http://") or raw.startswith("https://"):
        base_return = raw
    else:
        origin = (os.getenv("FRONTEND_ORIGIN") or "https://aln.ama8.uk").rstrip("/")
        path = "/" + raw.lstrip("/")
        base_return = f"{origin}{path}"
    delim = "&" if "?" in base_return else "?"
    return f"{base_return}{delim}payment={payment_key}&via={via}"


def start_online_payment(
    *,
    actor: User,
    data: dict[str, Any],
    gateway: QiCardGateway | None = None,
    notification_url: str = "",
) -> dict[str, Any]:
    if actor.role not in (Role.SHOP, Role.MANAGER, Role.ACCOUNTANT):
        raise ApiException(code=ErrorCode.FORBIDDEN, status_code=http_status.HTTP_403_FORBIDDEN)
    if not rules_of()["features"]["online_pay"]:
        raise ApiException(
            code=ErrorCode.FEATURE_OFF,
            message=("الدفع الإلكتروني متوقف الآن", "Online payment is turned off"),
            status_code=http_status.HTTP_403_FORBIDDEN,
        )

    client_ref = (str(data.get("key") or data.get("client_ref") or "")).strip() or None
    if client_ref:
        existing = OnlinePayment.objects.select_related("initiated_by", "customer").filter(client_ref=client_ref).first()
        if existing and (actor.role in (Role.MANAGER, Role.ACCOUNTANT) or existing.customer_id == get_active_customer_id(actor)):
            return serialize_online_payment(existing)

    if actor.role == Role.SHOP:
        cid = get_active_customer_id(actor)
        customer = Customer.objects.filter(id=cid).first() if cid else None
    else:
        customer = Customer.objects.filter(id=data.get("customer_id")).first()

    if not customer:
        raise ApiException(code=ErrorCode.NOT_FOUND, status_code=http_status.HTTP_404_NOT_FOUND)
    if not customer.active or customer.status != "active":
        raise ApiException(code=ErrorCode.FORBIDDEN, message=("حساب الزبون غير مفعل", "Customer account is inactive"), status_code=http_status.HTTP_403_FORBIDDEN)

    try:
        amount = int(data.get("amount"))
    except (TypeError, ValueError) as exc:
        msg = ("أدخل مبلغاً صحيحاً", "Enter a valid amount")
        raise ApiException(code=ErrorCode.VALIDATION, message=msg, errors={"amount": msg}) from exc
    if amount <= 0:
        msg = ("المبلغ يجب أن يكون أكبر من صفر", "Amount must be positive")
        raise ApiException(code=ErrorCode.VALIDATION, message=msg, errors={"amount": msg})

    provider = str(data.get("provider") or "qi").strip().lower()
    if provider not in ("qi", "zaincash"):
        provider = "qi"

    if provider == "zaincash" and amount < 1000:
        msg = ("زين كاش يقبل الدفع من 1,000 د.ع فأكثر", "ZainCash accepts payments from 1,000 IQD")
        raise ApiException(code=ErrorCode.VALIDATION, message=msg, errors={"amount": msg})

    finish_url = str(data.get("return_url") or "/statement").strip()

    if provider == "zaincash":
        from integrations.zaincash.client import get_zaincash_client

        payment_id = f"PAY-ZC-{uuid4().hex[:12].upper()}"
        request_id = f"ZC-{customer.id}-{uuid4().hex[:14]}"
        back_url = _resolve_online_return_url(finish_url, payment_key=payment_id, via="zaincash")

        zc = get_zaincash_client()
        try:
            init_res = zc.init_transaction(
                amount=amount,
                order_id=payment_id,
                reference=str(uuid4()),
                return_url=back_url,
                lang="ar",
                service="تسديد دين - النهرين",
            )
        except Exception as exc:
            logger.exception("ZainCash init transaction failed: %s", exc)
            raise ApiException(
                code=ErrorCode.SERVICE_UNAVAILABLE,
                message=("تعذّر فتح الدفع مع زين كاش", "Failed to initialize payment with ZainCash"),
                status_code=http_status.HTTP_502_BAD_GATEWAY,
            ) from exc

        op = OnlinePayment.objects.create(
            request_id=request_id,
            payment_id=payment_id,
            provider="zaincash",
            gateway_id=init_res.get("transaction_id", ""),
            customer=customer,
            initiated_by=actor,
            amount=amount,
            status=OnlinePaymentStatus.CREATED,
            form_url=init_res.get("redirect_url", ""),
            return_url=back_url,
            client_ref=client_ref,
            raw_payload=init_res,
        )
        return serialize_online_payment(op)

    # Qi Card
    request_id = f"QI-{customer.id}-{uuid4().hex[:14]}"
    back_url = _resolve_online_return_url(finish_url, payment_key=request_id, via="qi")
    notif_url = notification_url or ""
    if not notif_url or (not notif_url.startswith("https://") and "testserver" not in notif_url):
        api_origin = (os.getenv("PUBLIC_API_ORIGIN") or "https://api.ama8.uk").rstrip("/")
        notif_url = f"{api_origin}/api/v1/webhooks/qi-card"

    gw = gateway or QiCardGateway()
    res = gw.create_payment(
        request_id=request_id,
        amount=amount,
        finish_url=back_url,
        notification_url=notif_url,
        customer_name=customer.name,
        customer_phone=customer.phone,
    )

    op = OnlinePayment.objects.create(
        request_id=request_id,
        payment_id=res.payment_id,
        provider="qi",
        customer=customer,
        initiated_by=actor,
        amount=amount,
        status=res.status,
        form_url=res.form_url,
        return_url=back_url,
        client_ref=client_ref,
        raw_payload=res.raw,
    )
    return serialize_online_payment(op)


@transaction.atomic
def settle_online_payment(
    *,
    online_payment_id: int,
    new_status: str,
    raw_payload: dict[str, Any] | None = None,
    masked_card: str = "",
) -> OnlinePayment:
    op = OnlinePayment.objects.select_for_update().select_related("customer", "initiated_by").get(id=online_payment_id)
    if raw_payload is not None:
        op.raw_payload = raw_payload

    # Never downgrade an already settled success payment
    if op.status == OnlinePaymentStatus.SUCCESS and op.recorded_payment_id is not None:
        op.save(update_fields=["raw_payload", "updated_at"])
        return op

    op.status = new_status
    if new_status == OnlinePaymentStatus.SUCCESS and op.recorded_payment_id is None:
        actor = op.initiated_by
        is_zc = (op.provider == "zaincash")
        gw_label = "ZainCash" if is_zc else "Qi Card"
        pay_id = op.gateway_id or op.payment_id
        pay = _allocate_payment_fifo(
            customer=op.customer,
            amount=op.amount,
            method=PaymentMethod.ONLINE,
            note=f"{gw_label} ({pay_id})",
            received_by=actor,
            received_by_name=actor.name if actor else op.customer.name,
            received_by_role=actor.role if actor else Role.SHOP,
            client_ref=f"{op.provider or 'qi'}-settle-{op.payment_id}",
            gateway={
                "provider": op.provider or "qi",
                "payment_id": pay_id,
                "card": masked_card or gw_label,
                "status": "success",
            },
        )
        op.recorded_payment = pay

    op.save(update_fields=["status", "recorded_payment", "raw_payload", "updated_at"])
    return op


def list_online_payments_for_actor(*, actor: User) -> list[dict[str, Any]]:
    if actor.role not in (Role.MANAGER, Role.ACCOUNTANT, Role.SHOP):
        raise ApiException(code=ErrorCode.FORBIDDEN, status_code=http_status.HTTP_403_FORBIDDEN)
    qs = OnlinePayment.objects.select_related("initiated_by", "customer").order_by("-id")
    if actor.role == Role.SHOP:
        cid = get_active_customer_id(actor)
        qs = qs.filter(customer_id=cid) if cid else qs.none()
    return [serialize_online_payment(op) for op in qs]


def get_online_payment_for_actor(
    *,
    actor: User,
    key: str,
    gateway: QiCardGateway | None = None,
    return_token: str | None = None,
) -> dict[str, Any]:
    if actor.role not in (Role.MANAGER, Role.ACCOUNTANT, Role.SHOP):
        raise ApiException(code=ErrorCode.FORBIDDEN, status_code=http_status.HTTP_403_FORBIDDEN)

    if str(key).isdigit():
        op = OnlinePayment.objects.select_related("initiated_by", "customer").filter(id=int(key)).first()
    else:
        op = OnlinePayment.objects.select_related("initiated_by", "customer").filter(payment_id=str(key)).first()
    if not op:
        op = OnlinePayment.objects.select_related("initiated_by", "customer").filter(gateway_id=str(key)).first()
    if not op:
        op = OnlinePayment.objects.select_related("initiated_by", "customer").filter(request_id=str(key)).first()
    if not op:
        raise ApiException(code=ErrorCode.NOT_FOUND, status_code=http_status.HTTP_404_NOT_FOUND)
    if actor.role == Role.SHOP and get_active_customer_id(actor) != op.customer_id:
        raise ApiException(code=ErrorCode.FORBIDDEN, status_code=http_status.HTTP_403_FORBIDDEN)

    if op.status == OnlinePaymentStatus.CREATED:
        if op.provider == "zaincash" and op.gateway_id:
            from integrations.zaincash.client import get_zaincash_client

            zc = get_zaincash_client()
            target_status = None
            wallet = ""

            if return_token:
                tok_info = zc.verify_return_token(return_token)
                if tok_info and tok_info.get("transaction_id") == op.gateway_id:
                    mapped = zc.map_status(tok_info.get("status"))
                    if mapped != OnlinePaymentStatus.CREATED:
                        target_status = mapped

            try:
                inq = zc.inquire_transaction(op.gateway_id)
                inq_status = zc.map_status(inq.get("status"))
                if inq_status != OnlinePaymentStatus.CREATED:
                    target_status = inq_status
                wallet = inq.get("phone") or ""
            except Exception as exc:
                logger.warning("ZainCash inquiry error for %s: %s", op.gateway_id, exc)

            if target_status and target_status != OnlinePaymentStatus.CREATED:
                hidden_wallet = f"{wallet[:5]}•••••{wallet[-3:]}" if (wallet and len(wallet) >= 8) else (wallet or "ZainCash")
                op = settle_online_payment(
                    online_payment_id=op.id,
                    new_status=target_status,
                    raw_payload={"status": target_status, "wallet": wallet},
                    masked_card=hidden_wallet,
                )
        else:
            gw = gateway or QiCardGateway()
            if gw.is_configured():
                status_res = gw.get_payment_status(op.payment_id)
                if status_res.status != op.status:
                    op = settle_online_payment(
                        online_payment_id=op.id,
                        new_status=status_res.status,
                        raw_payload=status_res.raw,
                        masked_card=status_res.masked_card,
                    )

    return serialize_online_payment(op)


def handle_qi_card_webhook(
    *,
    raw_body: bytes,
    headers: Mapping[str, str],
    gateway: QiCardGateway | None = None,
) -> dict[str, Any]:
    gw = gateway or QiCardGateway()
    parsed_for_sig: dict[str, Any] | None = None
    if raw_body:
        try:
            cand = json.loads(raw_body.decode("utf-8"))
            if isinstance(cand, dict):
                parsed_for_sig = cand
        except (UnicodeDecodeError, json.JSONDecodeError):
            parsed_for_sig = None

    sig_verified = gw.verify_webhook_signature(raw_body=raw_body, headers=headers, payload=parsed_for_sig)

    try:
        payload = json.loads(raw_body.decode("utf-8")) if raw_body else {}
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ApiException(code=ErrorCode.VALIDATION, message=("بيانات غير صالحة", "Invalid JSON payload")) from exc

    pid = str(payload.get("paymentId") or payload.get("id") or "").strip()
    req_id = str(payload.get("requestId") or "").strip()

    op = None
    if pid:
        op = OnlinePayment.objects.filter(payment_id=pid).first()
    if not op and req_id:
        op = OnlinePayment.objects.filter(request_id=req_id).first()
    if not op:
        raise ApiException(code=ErrorCode.NOT_FOUND, status_code=http_status.HTTP_404_NOT_FOUND)

    details = payload.get("details") if isinstance(payload.get("details"), dict) else {}
    pm = payload.get("paymentMethod") if isinstance(payload.get("paymentMethod"), dict) else {}
    masked_card = str(
        payload.get("maskedPan")
        or details.get("maskedPan")
        or pm.get("maskedPan")
        or payload.get("card")
        or ""
    ).strip()

    if sig_verified:
        new_status = gw.map_status(payload.get("status"))
        if new_status == OnlinePaymentStatus.CREATED and payload.get("canceled") is True:
            new_status = OnlinePaymentStatus.CANCELLED
        raw_payload = payload
    else:
        # In sandbox without webhook_secret/public_key, confirm authoritatively from Qi Card status API.
        status_res = gw.get_payment_status(op.payment_id)
        new_status = status_res.status
        masked_card = status_res.masked_card or masked_card
        raw_payload = status_res.raw

    op = settle_online_payment(
        online_payment_id=op.id,
        new_status=new_status,
        raw_payload=raw_payload,
        masked_card=masked_card,
    )
    return {"ok": True, "status": op.status, "payment_id": op.recorded_payment_id}
