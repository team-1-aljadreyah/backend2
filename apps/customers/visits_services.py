"""Visits («خط السير») business logic, GPS distance check, and compliance reporting."""
from __future__ import annotations

import math
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from django.db import transaction
from rest_framework import status as http_status

from apps.accounts.business_rules import rules_of
from apps.accounts.models import Role, User
from apps.common.exceptions import ApiException, ErrorCode
from apps.common.utils import baghdad_now, baghdad_today
from apps.customers.models import Customer, Visit
from apps.customers.services import serialize_customer
from apps.notifications.services import notify_role

VISIT_NEAR_M = 200
VALID_OUTCOMES = ("order", "payment", "no_order", "closed")
VALID_NO_ORDER_REASONS = ("stocked", "no_money", "price", "other")


def js_weekday(d: date) -> int:
    """Convert Python weekday (Mon=0..Sun=6) to JS Date.getDay() (Sun=0..Sat=6)."""
    return (d.weekday() + 1) % 7


def _haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> int:
    r = 6_371_000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlmb / 2.0) ** 2
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return int(round(r * c))


def serialize_visit(v: Visit) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": v.id,
        "rep": {"id": v.rep_id, "name": v.rep.name},
        "customer": {"id": v.customer_id, "name": v.customer.name, "area": v.customer.area},
        "day": v.day.isoformat(),
        "at": v.at.isoformat(),
        "outcome": v.outcome,
        "planned": bool(v.planned),
    }
    if v.reason:
        out["reason"] = v.reason
    if v.note:
        out["note"] = v.note
    if v.distance_m is not None:
        out["distance_m"] = v.distance_m
    if v.far is not None:
        out["far"] = bool(v.far)
    if v.order_id is not None:
        out["order_id"] = v.order_id
    return out


def list_visits_for_actor(*, actor: User, day_str: str | None, rep_id_raw: str | None) -> list[dict[str, Any]]:
    if day_str:
        try:
            target_day = date.fromisoformat(day_str.strip())
        except ValueError as exc:
            raise ApiException(code=ErrorCode.VALIDATION, message=("التاريخ غير صحيح", "Invalid date")) from exc
    else:
        target_day = baghdad_today()

    qs = Visit.objects.select_related("rep", "customer").filter(day=target_day).order_by("-at", "-id")
    if actor.role == Role.REP:
        qs = qs.filter(rep_id=actor.id)
    elif actor.role not in (Role.MANAGER, Role.ACCOUNTANT, Role.STAFF):
        raise ApiException(code=ErrorCode.FORBIDDEN, status_code=http_status.HTTP_403_FORBIDDEN)
    elif rep_id_raw:
        try:
            rid = int(rep_id_raw)
        except (TypeError, ValueError) as exc:
            raise ApiException(code=ErrorCode.VALIDATION, message=("معرّف المندوب غير صحيح", "Invalid rep_id")) from exc
        qs = qs.filter(rep_id=rid)

    return [serialize_visit(v) for v in qs]


@transaction.atomic
def record_visit(*, actor: User, data: dict[str, Any]) -> dict[str, Any]:
    if actor.role != Role.REP:
        raise ApiException(
            code=ErrorCode.FORBIDDEN,
            message=("فقط المندوب يسجل الزيارة", "Only reps record visits"),
            status_code=http_status.HTTP_403_FORBIDDEN,
        )

    cust_id = data.get("customer_id")
    cust = Customer.objects.filter(id=cust_id, active=True).first()
    if not cust:
        raise ApiException(code=ErrorCode.NOT_FOUND, status_code=http_status.HTTP_404_NOT_FOUND)
    if cust.rep_id and cust.rep_id != actor.id:
        raise ApiException(code=ErrorCode.FORBIDDEN, status_code=http_status.HTTP_403_FORBIDDEN)

    outcome = str(data.get("outcome") or "").strip()
    if outcome not in VALID_OUTCOMES:
        raise ApiException(code=ErrorCode.VALIDATION, message=("نتيجة الزيارة غير صحيحة", "Invalid visit outcome"))

    reason = str(data.get("reason") or "").strip()
    if outcome == "no_order" and reason and reason not in VALID_NO_ORDER_REASONS:
        raise ApiException(code=ErrorCode.VALIDATION, message=("السبب غير صحيح", "Invalid reason"))
    if outcome != "no_order":
        reason = ""

    note = str(data.get("note") or "").strip()[:500]
    lat_raw = data.get("lat")
    lng_raw = data.get("lng")
    lat_dec: Decimal | None = None
    lng_dec: Decimal | None = None
    distance_m: int | None = None
    far: bool | None = None

    rules = rules_of()
    near_m = int(rules.get("visit_near_m") or VISIT_NEAR_M)
    if lat_raw is not None and lng_raw is not None and lat_raw != "" and lng_raw != "":
        try:
            lat_f = float(lat_raw)
            lng_f = float(lng_raw)
            lat_dec = Decimal(str(round(lat_f, 6)))
            lng_dec = Decimal(str(round(lng_f, 6)))
        except (TypeError, ValueError) as exc:
            raise ApiException(code=ErrorCode.VALIDATION, message=("الإحداثيات غير صحيحة", "Invalid coordinates")) from exc
        if cust.lat is not None and cust.lng is not None:
            distance_m = _haversine_m(lat_f, lng_f, float(cust.lat), float(cust.lng))
            far = distance_m > near_m

    now = baghdad_now()
    today = now.date()
    planned = js_weekday(today) in (cust.visit_days or [])

    existing = Visit.objects.select_for_update().filter(rep=actor, customer=cust, day=today).first()
    if existing:
        existing.at = now
        existing.outcome = outcome
        existing.reason = reason
        existing.note = note
        existing.lat = lat_dec
        existing.lng = lng_dec
        existing.distance_m = distance_m
        existing.far = far
        existing.planned = planned
        existing.save()
        visit = existing
    else:
        visit = Visit.objects.create(
            rep=actor,
            customer=cust,
            day=today,
            at=now,
            outcome=outcome,
            reason=reason,
            note=note,
            lat=lat_dec,
            lng=lng_dec,
            distance_m=distance_m,
            far=far,
            planned=planned,
        )

    if far and rules.get("far_visit_alert"):
        notify_role(
            role=Role.MANAGER,
            kind="needs_approval",
            title_ar=f"زيارة بعيدة عن {cust.name}",
            title_en=f"Far visit at {cust.name}",
            body_ar=f"{actor.name} · {distance_m} م",
            body_en=f"{actor.name} · {distance_m} m",
            ref_type="customer",
            ref_id=cust.id,
        )

    visit = Visit.objects.select_related("rep", "customer").get(id=visit.id)
    return serialize_visit(visit)


def record_auto_visit(
    *,
    actor: User,
    customer: Customer,
    outcome: str,
    order_id: int | None = None,
) -> None:
    """Auto-record a rep visit when creating an order or payment for a shop today."""
    if actor.role != Role.REP or customer.walk_in:
        return
    now = baghdad_now()
    today = now.date()
    planned = js_weekday(today) in (customer.visit_days or [])
    existing = Visit.objects.filter(rep=actor, customer=customer, day=today).first()
    if existing:
        if existing.outcome == "order" and outcome != "order":
            return
        existing.at = now
        existing.outcome = outcome
        existing.reason = ""
        existing.planned = planned
        if order_id is not None:
            existing.order_id = order_id
        existing.save(update_fields=["at", "outcome", "reason", "planned", "order"])
    else:
        Visit.objects.create(
            rep=actor,
            customer=customer,
            day=today,
            at=now,
            outcome=outcome,
            planned=planned,
            order_id=order_id,
        )


@transaction.atomic
def set_customer_visit_days(*, actor: User, customer_id: int, raw_days: Any) -> dict[str, Any]:
    cust = Customer.objects.select_for_update().filter(id=customer_id).first()
    if not cust:
        raise ApiException(code=ErrorCode.NOT_FOUND, status_code=http_status.HTTP_404_NOT_FOUND)

    if actor.role == Role.REP:
        if cust.rep_id != actor.id:
            raise ApiException(code=ErrorCode.FORBIDDEN, status_code=http_status.HTTP_403_FORBIDDEN)
    elif actor.role not in (Role.MANAGER, Role.STAFF):
        raise ApiException(code=ErrorCode.FORBIDDEN, status_code=http_status.HTTP_403_FORBIDDEN)

    if not isinstance(raw_days, list):
        raise ApiException(code=ErrorCode.VALIDATION, message=("الأيام يجب أن تكون قائمة", "days must be a list"))

    cleaned: list[int] = []
    for d in raw_days:
        try:
            di = int(d)
            if di < 0 or di > 6:
                raise ValueError
        except (TypeError, ValueError) as exc:
            raise ApiException(code=ErrorCode.VALIDATION, message=("رقم اليوم غير صحيح (0-6)", "Invalid weekday (0-6)")) from exc
        if di not in cleaned:
            cleaned.append(di)

    cust.visit_days = cleaned
    cust.save(update_fields=["visit_days", "updated_at"])
    return serialize_customer(cust, actor=actor)


def build_visits_report(*, actor: User, from_str: str | None, to_str: str | None) -> dict[str, Any]:
    if actor.role not in (Role.MANAGER, Role.ACCOUNTANT, Role.STAFF):
        raise ApiException(code=ErrorCode.FORBIDDEN, status_code=http_status.HTTP_403_FORBIDDEN)

    today = baghdad_today()
    try:
        d_from = date.fromisoformat(from_str.strip()) if from_str else today - timedelta(days=6)
        d_to = date.fromisoformat(to_str.strip()) if to_str else today
    except ValueError as exc:
        raise ApiException(code=ErrorCode.VALIDATION, message=("التاريخ غير صحيح", "Invalid date")) from exc

    if d_to < d_from:
        d_from, d_to = d_to, d_from
    if (d_to - d_from).days > 90:
        d_from = d_to - timedelta(days=90)

    visits = list(
        Visit.objects.select_related("rep", "customer")
        .filter(day__gte=d_from, day__lte=d_to)
        .order_by("-at", "-id")
    )
    visit_map: dict[tuple[int, int, date], Visit] = {(v.rep_id, v.customer_id, v.day): v for v in visits}

    customers = list(
        Customer.objects.select_related("rep").filter(active=True, walk_in=False, rep__isnull=False)
    )

    rep_stats: dict[int, dict[str, Any]] = {}

    def _ensure_rep(rid: int, rname: str) -> dict[str, Any]:
        if rid not in rep_stats:
            rep_stats[rid] = {
                "rep": {"id": rid, "name": rname},
                "planned": 0,
                "visited": 0,
                "missed": 0,
                "remaining": 0,
                "extra": 0,
                "far": 0,
                "orders": 0,
            }
        return rep_stats[rid]

    missed_list: list[dict[str, Any]] = []
    cur = d_from
    while cur <= d_to:
        w = js_weekday(cur)
        for cust in customers:
            if w in (cust.visit_days or []) and cust.rep_id:
                st = _ensure_rep(cust.rep_id, cust.rep.name)
                st["planned"] += 1
                v = visit_map.get((cust.rep_id, cust.id, cur))
                if v:
                    st["visited"] += 1
                    if v.far:
                        st["far"] += 1
                    if v.outcome == "order":
                        st["orders"] += 1
                else:
                    if cur < today:
                        st["missed"] += 1
                        missed_list.append(
                            {
                                "day": cur.isoformat(),
                                "rep": {"id": cust.rep_id, "name": cust.rep.name},
                                "customer": {"id": cust.id, "name": cust.name, "area": cust.area},
                            }
                        )
                    else:
                        st["remaining"] += 1
        cur += timedelta(days=1)

    reasons: dict[str, int] = {"stocked": 0, "no_money": 0, "price": 0, "other": 0}
    far_list: list[dict[str, Any]] = []

    for v in visits:
        if v.reason in reasons:
            reasons[v.reason] += 1
        if v.far:
            far_list.append(serialize_visit(v))
        if not v.planned:
            st = _ensure_rep(v.rep_id, v.rep.name)
            st["extra"] += 1
            st["visited"] += 1
            if v.far:
                st["far"] += 1
            if v.outcome == "order":
                st["orders"] += 1

    missed_list.sort(key=lambda m: m["day"], reverse=True)
    reps_out = sorted(rep_stats.values(), key=lambda r: r["rep"]["id"])
    totals = {
        "planned": sum(r["planned"] for r in reps_out),
        "visited": sum(r["visited"] for r in reps_out),
        "missed": sum(r["missed"] for r in reps_out),
        "remaining": sum(r["remaining"] for r in reps_out),
        "extra": sum(r["extra"] for r in reps_out),
        "far": sum(r["far"] for r in reps_out),
        "orders": sum(r["orders"] for r in reps_out),
    }
    return {
        "from": d_from.isoformat(),
        "to": d_to.isoformat(),
        "totals": totals,
        "reps": reps_out,
        "reasons": reasons,
        "missed": missed_list,
        "far": far_list,
    }
