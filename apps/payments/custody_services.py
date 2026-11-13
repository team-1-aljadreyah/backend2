"""Cash custody («العهدة») services for rep cash holdings and handovers."""
from __future__ import annotations

from typing import Any

from django.db import transaction
from rest_framework import status as http_status

from apps.accounts.models import Role, User
from apps.audit.services import record_audit
from apps.common.exceptions import ApiException, ErrorCode
from apps.common.utils import BAGHDAD_TZ, baghdad_now, baghdad_today
from apps.notifications.services import notify_role, notify_user
from apps.payments.models import CashHandover, Payment, PaymentMethod


def serialize_handover(h: CashHandover) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": h.id,
        "number": h.number,
        "rep": {"id": h.rep_id, "name": h.rep.name, "role": h.rep.role},
        "declared": h.declared,
        "status": h.status,
        "created_at": h.created_at.isoformat(),
    }
    if h.received is not None:
        out["received"] = h.received
    if h.short is not None and h.short > 0:
        out["short"] = h.short
    if h.note:
        out["note"] = h.note
    if h.confirmed_at:
        out["confirmed_at"] = h.confirmed_at.isoformat()
    if h.confirmed_by_id:
        out["confirmed_by"] = {
            "id": h.confirmed_by_id,
            "name": h.confirmed_by.name,
            "role": h.confirmed_by.role,
        }
    return out


def _compute_custody_row(rep: User) -> dict[str, Any]:
    today = baghdad_today()
    cash_payments = list(
        Payment.objects.filter(
            received_by_id=rep.id,
            method=PaymentMethod.CASH,
            reversed=False,
        )
        .select_related("customer")
        .order_by("created_at", "id")
    )
    collected = sum(p.amount for p in cash_payments)
    today_total = sum(p.amount for p in cash_payments if p.created_at.astimezone(BAGHDAD_TZ).date() == today)

    handovers = list(
        CashHandover.objects.filter(rep_id=rep.id)
        .select_related("rep", "confirmed_by")
        .order_by("-id")
    )
    confirmed = [h for h in handovers if h.status == "confirmed"]
    handed = sum(h.received or 0 for h in confirmed)
    short_total = sum(h.short or 0 for h in confirmed if (h.short or 0) > 0)
    holding = collected - handed

    pending_h = next((h for h in handovers if h.status == "pending"), None)
    last_h = max((h.confirmed_at for h in confirmed if h.confirmed_at), default=None)

    oldest_at: str | None = None
    if holding > 0:
        covered = handed
        for p in cash_payments:
            if covered >= p.amount:
                covered -= p.amount
            else:
                oldest_at = p.created_at.isoformat()
                break

    row: dict[str, Any] = {
        "rep": {"id": rep.id, "name": rep.name, "role": rep.role, "phone": rep.phone},
        "collected": collected,
        "handed": handed,
        "holding": holding,
        "today": today_total,
        "short_total": short_total,
    }
    if pending_h:
        row["pending"] = serialize_handover(pending_h)
    if oldest_at:
        row["oldest_at"] = oldest_at
    if last_h:
        row["last_handover_at"] = last_h.isoformat()
    return row


def list_custody_for_actor(*, actor: User) -> list[dict[str, Any]]:
    if actor.role == Role.REP:
        return [_compute_custody_row(actor)]
    if actor.role not in (Role.MANAGER, Role.ACCOUNTANT):
        raise ApiException(code=ErrorCode.FORBIDDEN, status_code=http_status.HTTP_403_FORBIDDEN)

    reps = list(User.objects.filter(role=Role.REP, deleted=False).order_by("id"))
    rows = [_compute_custody_row(r) for r in reps]
    return [r for r in rows if r["collected"] > 0 or r["holding"] != 0 or r.get("pending") or User.objects.filter(id=r["rep"]["id"], active=True).exists()]


def get_custody_log_for_actor(*, actor: User, rep_id: int) -> dict[str, Any]:
    if actor.role == Role.REP:
        if actor.id != rep_id:
            raise ApiException(code=ErrorCode.FORBIDDEN, status_code=http_status.HTTP_403_FORBIDDEN)
    elif actor.role not in (Role.MANAGER, Role.ACCOUNTANT):
        raise ApiException(code=ErrorCode.FORBIDDEN, status_code=http_status.HTTP_403_FORBIDDEN)

    rep = User.objects.filter(id=rep_id, role=Role.REP).first()
    if not rep:
        raise ApiException(code=ErrorCode.NOT_FOUND, status_code=http_status.HTTP_404_NOT_FOUND)

    row = _compute_custody_row(rep)
    cash_payments = list(
        Payment.objects.filter(
            received_by_id=rep.id,
            method=PaymentMethod.CASH,
            reversed=False,
        )
        .select_related("customer")
        .order_by("-created_at", "-id")
    )
    handovers = list(
        CashHandover.objects.filter(rep_id=rep.id)
        .select_related("rep", "confirmed_by")
        .order_by("-id")
    )

    entries: list[dict[str, Any]] = []
    for p in cash_payments:
        entries.append(
            {
                "at": p.created_at.isoformat(),
                "kind": "cash",
                "amount": p.amount,
                "number": p.number,
                "customer": p.customer.name if p.customer else "",
                "link": f"/payments/{p.id}",
            }
        )
    for h in handovers:
        if h.status == "confirmed":
            entry: dict[str, Any] = {
                "at": (h.confirmed_at or h.created_at).isoformat(),
                "kind": "handover",
                "amount": -(h.received or 0),
                "number": h.number,
                "declared": h.declared,
            }
            if h.short and h.short > 0:
                entry["short"] = h.short
            entries.append(entry)

    entries.sort(key=lambda e: e["at"], reverse=True)
    return {
        "row": row,
        "entries": entries,
        "handovers": [serialize_handover(h) for h in handovers],
    }


@transaction.atomic
def create_cash_handover(*, actor: User, data: dict[str, Any]) -> dict[str, Any]:
    if actor.role == Role.REP:
        rep = actor
        is_direct = False
    elif actor.role in (Role.MANAGER, Role.ACCOUNTANT):
        rep_id = data.get("rep_id")
        rep = User.objects.filter(id=rep_id, role=Role.REP).first()
        if not rep:
            raise ApiException(code=ErrorCode.NOT_FOUND, status_code=http_status.HTTP_404_NOT_FOUND)
        is_direct = True
    else:
        raise ApiException(code=ErrorCode.FORBIDDEN, status_code=http_status.HTTP_403_FORBIDDEN)

    try:
        amount = int(data.get("amount") or 0)
        if amount <= 0:
            raise ValueError
    except (TypeError, ValueError) as exc:
        raise ApiException(code=ErrorCode.VALIDATION, message=("المبلغ يجب أن يكون موجباً", "Amount must be positive")) from exc

    note = str(data.get("note") or "").strip()[:300]
    row = _compute_custody_row(rep)
    if not is_direct and CashHandover.objects.select_for_update().filter(rep=rep, status="pending").exists():
        raise ApiException(
            code=ErrorCode.BAD_STATE,
            message=("لديك تسليم قيد الانتظار بالفعل", "You already have a pending handover"),
        )
    if amount > max(0, row["holding"]):
        raise ApiException(
            code=ErrorCode.VALIDATION,
            message=("المبلغ أكبر من العهدة الحالية", "Amount exceeds current holding"),
        )

    now = baghdad_now()
    h = CashHandover.objects.create(
        number=f"HND-TMP-{rep.id}",
        rep=rep,
        declared=amount,
        received=amount if is_direct else None,
        short=0 if is_direct else None,
        status="confirmed" if is_direct else "pending",
        note=note,
        confirmed_at=now if is_direct else None,
        confirmed_by=actor if is_direct else None,
    )
    h.number = f"HND-{h.id:06d}"
    h.save(update_fields=["number"])

    if is_direct:
        notify_user(
            user_id=rep.id,
            kind="custody",
            title_ar=f"استُلمت عهدة {h.number} ({amount:,} د.ع)",
            title_en=f"Handover {h.number} received ({amount:,} IQD)",
            ref_type="custody",
            ref_id=h.id,
        )
    else:
        notify_role(
            role=Role.ACCOUNTANT,
            kind="custody",
            title_ar=f"تسليم عهدة جديد {h.number} من {rep.name}",
            title_en=f"New custody handover {h.number} from {rep.name}",
            ref_type="custody",
            ref_id=h.id,
        )

    record_audit(
        actor=actor,
        action="custody.handover",
        entity_type="cash_handover",
        entity_id=h.id,
        summary=f"{h.number} declared {amount} ({h.status})",
    )
    return serialize_handover(CashHandover.objects.select_related("rep", "confirmed_by").get(id=h.id))


@transaction.atomic
def confirm_cash_handover(*, actor: User, handover_id: int, received_raw: Any) -> dict[str, Any]:
    if actor.role not in (Role.MANAGER, Role.ACCOUNTANT):
        raise ApiException(code=ErrorCode.FORBIDDEN, status_code=http_status.HTTP_403_FORBIDDEN)

    h = CashHandover.objects.select_for_update().select_related("rep").filter(id=handover_id).first()
    if not h:
        raise ApiException(code=ErrorCode.NOT_FOUND, status_code=http_status.HTTP_404_NOT_FOUND)
    if h.status != "pending":
        raise ApiException(code=ErrorCode.BAD_STATE, message=("التسليم غير معلق", "Handover is not pending"))

    try:
        received = int(received_raw)
        if received < 0 or received > h.declared:
            raise ValueError
    except (TypeError, ValueError) as exc:
        raise ApiException(
            code=ErrorCode.VALIDATION,
            message=("المبلغ المستلم يجب أن يكون بين 0 والمبلغ المعلن", "Received must be between 0 and declared"),
        ) from exc

    short = h.declared - received
    h.received = received
    h.short = short
    h.status = "confirmed"
    h.confirmed_by = actor
    h.confirmed_at = baghdad_now()
    h.save(update_fields=["received", "short", "status", "confirmed_by", "confirmed_at"])

    notify_user(
        user_id=h.rep_id,
        kind="custody",
        title_ar=f"تأكيد عهدة {h.number}: استُلم {received:,} د.ع" + (f" (نقص {short:,})" if short else ""),
        title_en=f"Handover {h.number} confirmed: {received:,} IQD" + (f" (short {short:,})" if short else ""),
        ref_type="custody",
        ref_id=h.id,
    )
    record_audit(
        actor=actor,
        action="custody.confirm",
        entity_type="cash_handover",
        entity_id=h.id,
        summary=f"Confirmed {h.number}: received={received}, short={short}",
    )
    return serialize_handover(CashHandover.objects.select_related("rep", "confirmed_by").get(id=h.id))


@transaction.atomic
def cancel_cash_handover(*, actor: User, handover_id: int) -> dict[str, Any]:
    h = CashHandover.objects.select_for_update().select_related("rep").filter(id=handover_id).first()
    if not h:
        raise ApiException(code=ErrorCode.NOT_FOUND, status_code=http_status.HTTP_404_NOT_FOUND)
    if actor.role == Role.REP:
        if h.rep_id != actor.id:
            raise ApiException(code=ErrorCode.FORBIDDEN, status_code=http_status.HTTP_403_FORBIDDEN)
    elif actor.role not in (Role.MANAGER, Role.ACCOUNTANT):
        raise ApiException(code=ErrorCode.FORBIDDEN, status_code=http_status.HTTP_403_FORBIDDEN)

    if h.status != "pending":
        raise ApiException(code=ErrorCode.BAD_STATE, message=("التسليم غير معلق", "Handover is not pending"))

    h.status = "cancelled"
    h.confirmed_by = actor
    h.confirmed_at = baghdad_now()
    h.save(update_fields=["status", "confirmed_by", "confirmed_at"])

    if actor.role != Role.REP:
        notify_user(
            user_id=h.rep_id,
            kind="custody",
            title_ar=f"رُفض تسليم العهدة {h.number}",
            title_en=f"Handover {h.number} cancelled",
            ref_type="custody",
            ref_id=h.id,
        )
    record_audit(
        actor=actor,
        action="custody.cancel",
        entity_type="cash_handover",
        entity_id=h.id,
        summary=f"Cancelled {h.number}",
    )
    return serialize_handover(CashHandover.objects.select_related("rep", "confirmed_by").get(id=h.id))
