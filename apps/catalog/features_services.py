"""Services for Stock Alerts («أعلمني عند التوفر»), Clearances («عروض التصريف»), and Dated Prices («تغيير الأسعار من تاريخ»)."""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from django.db import transaction
from django.db.models import Sum
from rest_framework import status as http_status

from apps.accounts.models import PriceTier, Role, User
from apps.accounts.permissions import get_active_customer_id
from apps.audit.services import record_audit
from apps.catalog.models import Clearance, Item, ItemPrice, PriceChange, ScheduledPrice, StockAlert
from apps.common.exceptions import ApiException, ErrorCode
from apps.common.utils import baghdad_now, baghdad_today, round_250
from apps.customers.models import ShopMembership
from apps.inventory.models import Batch
from apps.notifications.services import notify_user
from apps.orders.models import Order, OrderStatus

# ---------------------------------------------------------------------------
# 1. Back in Stock («أعلمني عند التوفر»)
# ---------------------------------------------------------------------------

def get_my_stock_alerts(*, actor: User) -> list[int]:
    if actor.role != Role.SHOP:
        raise ApiException(code=ErrorCode.FORBIDDEN, status_code=http_status.HTTP_403_FORBIDDEN)
    cid = get_active_customer_id(actor)
    if not cid:
        return []
    return list(StockAlert.objects.filter(customer_id=cid).order_by("id").values_list("item_id", flat=True))


@transaction.atomic
def set_stock_alert(*, actor: User, item_id: int, on: bool) -> list[int]:
    if actor.role != Role.SHOP:
        raise ApiException(code=ErrorCode.FORBIDDEN, status_code=http_status.HTTP_403_FORBIDDEN)
    cid = get_active_customer_id(actor)
    if not cid:
        raise ApiException(code=ErrorCode.NOT_FOUND, status_code=http_status.HTTP_404_NOT_FOUND)
    item = Item.objects.filter(id=item_id, active=True, deleted=False).first()
    if not item:
        raise ApiException(code=ErrorCode.NOT_FOUND, status_code=http_status.HTTP_404_NOT_FOUND)

    if on:
        StockAlert.objects.get_or_create(customer_id=cid, item_id=item.id)
    else:
        StockAlert.objects.filter(customer_id=cid, item_id=item.id).delete()

    return list(StockAlert.objects.filter(customer_id=cid).order_by("id").values_list("item_id", flat=True))


def get_stock_waitlist(*, actor: User) -> list[dict[str, Any]]:
    if actor.role not in (Role.MANAGER, Role.ACCOUNTANT, Role.WAREHOUSE):
        raise ApiException(code=ErrorCode.FORBIDDEN, status_code=http_status.HTTP_403_FORBIDDEN)

    alerts = list(
        StockAlert.objects.select_related("item", "customer")
        .filter(item__active=True, item__deleted=False)
        .order_by("created_at", "id")
    )
    by_item: dict[int, dict[str, Any]] = {}
    for a in alerts:
        entry = by_item.get(a.item_id)
        if not entry:
            entry = {
                "item": {"id": a.item_id, "name": a.item.name, "name_en": a.item.name_en},
                "shops": 0,
                "since": a.created_at.isoformat(),
                "customers": [],
            }
            by_item[a.item_id] = entry
        entry["shops"] += 1
        entry["customers"].append({"id": a.customer_id, "name": a.customer.name})

    rows = list(by_item.values())
    rows.sort(key=lambda r: (-r["shops"], r["since"]))
    return rows


def trigger_back_in_stock_alerts(item_ids: list[int]) -> int:
    """Notify waiting shops when items become sellable (> 0 non-expired unheld stock)."""
    if not item_ids:
        return 0
    unique_ids = list({int(i) for i in item_ids})
    alerts = list(
        StockAlert.objects.select_related("item", "customer")
        .filter(item_id__in=unique_ids)
        .order_by("id")
    )
    if not alerts:
        return 0

    today = baghdad_today()
    stocked_ids = set(
        Batch.objects.filter(
            item_id__in=unique_ids,
            qty_left__gt=0,
            held_at__isnull=True,
            expiry_date__gt=today,
        )
        .values("item_id")
        .annotate(total=Sum("qty_left"))
        .filter(total__gt=0)
        .values_list("item_id", flat=True)
    )
    if not stocked_ids:
        return 0

    to_notify = [a for a in alerts if a.item_id in stocked_ids]
    if not to_notify:
        return 0

    cust_ids = list({a.customer_id for a in to_notify})
    user_ids_by_cust: dict[int, list[int]] = {}
    for cid, uid in ShopMembership.objects.filter(
        customer_id__in=cust_ids, user__active=True, user__deleted=False
    ).values_list("customer_id", "user_id"):
        user_ids_by_cust.setdefault(cid, []).append(uid)

    sent = 0
    delete_ids: list[int] = []
    for a in to_notify:
        for uid in user_ids_by_cust.get(a.customer_id, []):
            notify_user(
                user_id=uid,
                kind="back_in_stock",
                title_ar=f"توفّر الصنف: {a.item.name}",
                title_en=f"Back in stock: {a.item.name_en}",
                body_ar="أصبح الصنف متوفراً للطلب الآن",
                body_en="This item is now available to order",
                ref_type="item",
                ref_id=a.item_id,
            )
            sent += 1
        delete_ids.append(a.id)

    if delete_ids:
        StockAlert.objects.filter(id__in=delete_ids).delete()
    return sent


# ---------------------------------------------------------------------------
# 2. Clearances («عروض التصريف»)
# ---------------------------------------------------------------------------

def sync_clearances_status() -> None:
    today = baghdad_today()
    now = baghdad_now()
    active = list(Clearance.objects.select_related("batch").filter(status="active"))
    for c in active:
        if c.batch.qty_left <= 0:
            c.status = "sold"
            c.ended_at = now
            c.save(update_fields=["status", "ended_at"])
        elif c.until < today:
            c.status = "expired"
            c.ended_at = now
            c.save(update_fields=["status", "ended_at"])


def active_clearance_map(item_ids: list[int] | None = None) -> dict[int, dict[str, Any]]:
    """Return {item_id: ItemClearance} for active clearances."""
    sync_clearances_status()
    today = baghdad_today()
    qs = Clearance.objects.select_related("batch").filter(
        status="active",
        until__gte=today,
        batch__qty_left__gt=0,
        batch__held_at__isnull=True,
    )
    if item_ids is not None:
        qs = qs.filter(item_id__in=item_ids)
    out: dict[int, dict[str, Any]] = {}
    for c in qs.order_by("-percent", "until", "id"):
        if c.item_id not in out:
            out[c.item_id] = {
                "id": c.id,
                "percent": c.percent,
                "warehouse_id": c.warehouse_id,
                "until": c.until.isoformat(),
                "left": c.batch.qty_left,
            }
    return out


def serialize_clearance(c: Clearance) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": c.id,
        "item": {"id": c.item_id, "name": c.item.name, "name_en": c.item.name_en},
        "batch_id": c.batch_id,
        "warehouse_id": c.warehouse_id,
        "percent": c.percent,
        "qty": c.qty,
        "left": max(0, c.batch.qty_left),
        "until": c.until.isoformat(),
        "expiry_date": c.expiry_date.isoformat(),
        "status": c.status,
        "created_at": c.created_at.isoformat(),
        "by": {"id": c.created_by_id, "name": c.created_by.name, "role": c.created_by.role},
    }
    if c.ended_at:
        out["ended_at"] = c.ended_at.isoformat()
    return out


def list_clearances_for_actor(*, actor: User) -> list[dict[str, Any]]:
    if actor.role not in (Role.MANAGER, Role.ACCOUNTANT, Role.WAREHOUSE):
        raise ApiException(code=ErrorCode.FORBIDDEN, status_code=http_status.HTTP_403_FORBIDDEN)
    sync_clearances_status()
    rows = list(Clearance.objects.select_related("item", "batch", "created_by").order_by("-id"))
    rows.sort(key=lambda c: (0 if c.status == "active" else 1, -c.id))
    return [serialize_clearance(c) for c in rows]


@transaction.atomic
def start_clearance(*, actor: User, data: dict[str, Any]) -> dict[str, Any]:
    if actor.role != Role.MANAGER:
        raise ApiException(code=ErrorCode.FORBIDDEN, status_code=http_status.HTTP_403_FORBIDDEN)

    sync_clearances_status()
    batch = Batch.objects.select_for_update().select_related("item").filter(id=data.get("batch_id")).first()
    if not batch:
        raise ApiException(code=ErrorCode.NOT_FOUND, status_code=http_status.HTTP_404_NOT_FOUND)
    if batch.qty_left <= 0 or batch.is_held:
        raise ApiException(code=ErrorCode.BAD_STATE, message=("الوجبة غير متاحة للتصريف", "Batch is empty or held"))

    today = baghdad_today()
    until_day = batch.expiry_date - timedelta(days=1)
    if until_day < today:
        raise ApiException(code=ErrorCode.BAD_STATE, message=("الوجبة منتهية الصلاحية", "Batch is already expired"))

    try:
        percent = int(data.get("percent") or 0)
        if percent < 5 or percent > 70:
            raise ValueError
    except (TypeError, ValueError) as exc:
        raise ApiException(code=ErrorCode.VALIDATION, message=("نسبة الخصم يجب أن تكون 5-70%", "Percent must be 5-70")) from exc

    if Clearance.objects.filter(batch_id=batch.id, status="active").exists():
        raise ApiException(code=ErrorCode.BAD_STATE, message=("يوجد عرض تصريف فعال لهذه الوجبة", "Clearance already active for this batch"))

    c = Clearance.objects.create(
        item=batch.item,
        batch=batch,
        warehouse_id=batch.warehouse_id,
        percent=percent,
        qty=batch.qty_left,
        until=until_day,
        expiry_date=batch.expiry_date,
        status="active",
        created_by=actor,
    )

    for uid in User.objects.filter(role=Role.SHOP, active=True, deleted=False).values_list("id", flat=True):
        notify_user(
            user_id=uid,
            kind="clearance",
            title_ar=f"عرض تصريف {percent}% على {batch.item.name}",
            title_en=f"Clearance {percent}% off: {batch.item.name_en}",
            body_ar=f"ساري حتى {until_day.isoformat()}",
            body_en=f"Valid until {until_day.isoformat()}",
            ref_type="item",
            ref_id=batch.item_id,
        )

    record_audit(
        actor=actor,
        action="clearance.start",
        entity_type="clearance",
        entity_id=c.id,
        summary=f"Started {percent}% clearance on batch #{batch.id} ({batch.item.name})",
    )
    return serialize_clearance(Clearance.objects.select_related("item", "batch", "created_by").get(id=c.id))


@transaction.atomic
def cancel_clearance(*, actor: User, clearance_id: int) -> dict[str, Any]:
    if actor.role != Role.MANAGER:
        raise ApiException(code=ErrorCode.FORBIDDEN, status_code=http_status.HTTP_403_FORBIDDEN)
    c = Clearance.objects.select_for_update().select_related("item", "batch", "created_by").filter(id=clearance_id).first()
    if not c:
        raise ApiException(code=ErrorCode.NOT_FOUND, status_code=http_status.HTTP_404_NOT_FOUND)
    if c.status != "active":
        raise ApiException(code=ErrorCode.BAD_STATE, message=("العرض غير فعال", "Clearance is not active"))

    c.status = "cancelled"
    c.ended_at = baghdad_now()
    c.save(update_fields=["status", "ended_at"])
    record_audit(
        actor=actor,
        action="clearance.cancel",
        entity_type="clearance",
        entity_id=c.id,
        summary=f"Cancelled clearance #{c.id}",
    )
    return serialize_clearance(c)


# ---------------------------------------------------------------------------
# 3. Dated Prices («تغيير الأسعار من تاريخ»)
# ---------------------------------------------------------------------------

def _notify_recent_customers_price_change(items_meta: list[dict[str, Any]], start_str: str, is_rise: bool) -> int:
    cutoff = baghdad_now() - timedelta(days=60)
    cust_ids = list(
        Order.objects.filter(created_at__gte=cutoff)
        .exclude(status=OrderStatus.CANCELLED)
        .values_list("customer_id", flat=True)
        .distinct()
    )
    if not cust_ids:
        return 0
    user_ids = list(
        ShopMembership.objects.filter(
            customer_id__in=cust_ids, user__active=True, user__deleted=False
        )
        .values_list("user_id", flat=True)
        .distinct()
    )
    names_ar = "، ".join(m["name"] for m in items_meta[:3]) + ("..." if len(items_meta) > 3 else "")
    names_en = ", ".join(m["name_en"] for m in items_meta[:3]) + ("..." if len(items_meta) > 3 else "")
    for uid in user_ids:
        notify_user(
            user_id=uid,
            kind="price_change",
            title_ar=f"{'تنبيه ارتفاع أسعار' if is_rise else 'تخفيض أسعار'} من {start_str}",
            title_en=f"{'Price increase' if is_rise else 'Price drop'} from {start_str}",
            body_ar=names_ar,
            body_en=names_en,
            ref_type="catalog",
            ref_id=items_meta[0]["id"] if items_meta else "",
        )
    return len(user_ids)


def _apply_scheduled_price_obj(sp: ScheduledPrice, *, actor: User | None) -> None:
    now = baghdad_now()
    item_ids = [int(m["id"]) for m in (sp.items or [])]
    if not item_ids:
        return
    if sp.percent is not None:
        factor = Decimal("1") + (Decimal(sp.percent) / Decimal("100"))
        for ip in ItemPrice.objects.select_for_update().filter(item_id__in=item_ids).order_by("id"):
            new_c = max(250, round_250(int((Decimal(ip.carton_price) * factor).quantize(Decimal("1")))))
            new_p = max(0, round_250(int((Decimal(ip.piece_price) * factor).quantize(Decimal("1")))))
            if new_c != ip.carton_price or new_p != ip.piece_price:
                ip.carton_price = new_c
                ip.piece_price = new_p
                ip.save(update_fields=["carton_price", "piece_price"])
                PriceChange.objects.create(
                    item_id=ip.item_id,
                    tier_id=ip.tier_id,
                    carton_price=new_c,
                    piece_price=new_p,
                    changed_by=actor or sp.created_by,
                    effective_from=now,
                )
    elif sp.prices:
        iid = item_ids[0]
        for row in sp.prices:
            tid = int(row["tier_id"])
            new_c = int(row["carton_price"])
            new_p = int(row["piece_price"])
            ip, _ = ItemPrice.objects.select_for_update().get_or_create(
                item_id=iid, tier_id=tid, defaults={"carton_price": new_c, "piece_price": new_p}
            )
            ip.carton_price = new_c
            ip.piece_price = new_p
            ip.save(update_fields=["carton_price", "piece_price"])
            PriceChange.objects.create(
                item_id=iid,
                tier_id=tid,
                carton_price=new_c,
                piece_price=new_p,
                changed_by=actor or sp.created_by,
                effective_from=now,
            )

    has_rise = any(m.get("direction") in ("up", "mixed") for m in (sp.items or []))
    if not has_rise and sp.told == 0:
        sp.told = _notify_recent_customers_price_change(sp.items or [], sp.start.isoformat(), is_rise=False)

    sp.status = "applied"
    sp.applied_at = now
    sp.save(update_fields=["status", "applied_at", "told"])


@transaction.atomic
def apply_due_scheduled_prices() -> int:
    today = baghdad_today()
    due = list(
        ScheduledPrice.objects.select_for_update()
        .filter(status="scheduled", start__lte=today)
        .order_by("start", "id")
    )
    for sp in due:
        _apply_scheduled_price_obj(sp, actor=sp.created_by)
    return len(due)


def serialize_scheduled_price(sp: ScheduledPrice) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": sp.id,
        "start": sp.start.isoformat(),
        "items": list(sp.items or []),
        "status": sp.status,
        "created_at": sp.created_at.isoformat(),
        "by": {"id": sp.created_by_id, "name": sp.created_by.name, "role": sp.created_by.role},
        "told": sp.told,
    }
    if sp.percent is not None:
        pct = sp.percent
        out["percent"] = int(pct) if pct == int(pct) else float(pct)
    if sp.prices is not None:
        out["prices"] = list(sp.prices)
    if sp.note:
        out["note"] = sp.note
    if sp.applied_at:
        out["applied_at"] = sp.applied_at.isoformat()
    return out


def list_scheduled_prices_for_actor(*, actor: User) -> list[dict[str, Any]]:
    if actor.role not in (Role.MANAGER, Role.ACCOUNTANT):
        raise ApiException(code=ErrorCode.FORBIDDEN, status_code=http_status.HTTP_403_FORBIDDEN)
    apply_due_scheduled_prices()
    rows = list(ScheduledPrice.objects.select_related("created_by").order_by("-id"))
    rows.sort(key=lambda sp: (0 if sp.status == "scheduled" else 1, -sp.id))
    return [serialize_scheduled_price(sp) for sp in rows]


@transaction.atomic
def create_scheduled_price(*, actor: User, data: dict[str, Any]) -> dict[str, Any]:
    if actor.role != Role.MANAGER:
        raise ApiException(code=ErrorCode.FORBIDDEN, status_code=http_status.HTTP_403_FORBIDDEN)

    raw_ids = data.get("item_ids")
    if not isinstance(raw_ids, list) or not raw_ids:
        raise ApiException(code=ErrorCode.VALIDATION, message=("اختر صنفاً واحداً على الأقل", "Select at least one item"))
    item_ids = [int(i) for i in raw_ids]
    items = list(Item.objects.prefetch_related("prices").filter(id__in=item_ids, deleted=False).order_by("id"))
    if len(items) != len(set(item_ids)):
        raise ApiException(code=ErrorCode.NOT_FOUND, status_code=http_status.HTTP_404_NOT_FOUND)

    try:
        start_day = date.fromisoformat(str(data.get("start") or "").strip())
    except ValueError as exc:
        raise ApiException(code=ErrorCode.VALIDATION, message=("التاريخ غير صحيح", "Invalid start date")) from exc

    today = baghdad_today()
    if start_day < today:
        raise ApiException(code=ErrorCode.VALIDATION, message=("التاريخ يجب أن يكون اليوم أو لاحقاً", "Start date must be today or future"))

    percent_raw = data.get("percent")
    prices_raw = data.get("prices")
    pct_dec: Decimal | None = None
    prices_clean: list[dict[str, int]] | None = None
    items_meta: list[dict[str, Any]] = []

    if percent_raw is not None and percent_raw != "":
        try:
            pct_dec = Decimal(str(percent_raw))
            if pct_dec == 0 or pct_dec < Decimal("-80") or pct_dec > Decimal("300"):
                raise ValueError
        except (Exception, ValueError) as exc:
            raise ApiException(code=ErrorCode.VALIDATION, message=("النسبة غير صحيحة", "Invalid percent")) from exc
        direction = "up" if pct_dec > 0 else "down"
        for it in items:
            items_meta.append({"id": it.id, "name": it.name, "name_en": it.name_en, "direction": direction})
    elif isinstance(prices_raw, list) and len(items) == 1:
        it = items[0]
        cur_map = {ip.tier_id: (ip.carton_price, ip.piece_price) for ip in it.prices.all()}
        prices_clean = []
        ups, downs = 0, 0
        for r in prices_raw:
            tid = int(r["tier_id"])
            cp = int(r["carton_price"])
            pp = int(r["piece_price"])
            if cp < 0 or pp < 0:
                raise ApiException(code=ErrorCode.VALIDATION, message=("السعر سالب", "Negative price"))
            prices_clean.append({"tier_id": tid, "carton_price": cp, "piece_price": pp})
            old_cp, old_pp = cur_map.get(tid, (cp, pp))
            if cp > old_cp or pp > old_pp:
                ups += 1
            if cp < old_cp or pp < old_pp:
                downs += 1
        direction = "mixed" if (ups and downs) else ("up" if ups else "down")
        items_meta.append({"id": it.id, "name": it.name, "name_en": it.name_en, "direction": direction})
    else:
        raise ApiException(code=ErrorCode.VALIDATION, message=("أدخل نسبة مئوية أو أسعار صنف واحد", "Provide percent or single-item prices"))

    note = str(data.get("note") or "").strip()[:300]
    sp = ScheduledPrice.objects.create(
        start=start_day,
        items=items_meta,
        percent=pct_dec,
        prices=prices_clean,
        status="scheduled",
        note=note,
        created_by=actor,
    )

    has_rise = any(m["direction"] in ("up", "mixed") for m in items_meta)
    if has_rise:
        sp.told = _notify_recent_customers_price_change(items_meta, start_day.isoformat(), is_rise=True)
        sp.save(update_fields=["told"])

    if start_day <= today:
        _apply_scheduled_price_obj(sp, actor=actor)

    record_audit(
        actor=actor,
        action="price_schedule.create",
        entity_type="scheduled_price",
        entity_id=sp.id,
        summary=f"Scheduled price change #{sp.id} for {start_day.isoformat()}",
    )
    return serialize_scheduled_price(ScheduledPrice.objects.select_related("created_by").get(id=sp.id))


@transaction.atomic
def cancel_scheduled_price(*, actor: User, schedule_id: int) -> dict[str, Any]:
    if actor.role != Role.MANAGER:
        raise ApiException(code=ErrorCode.FORBIDDEN, status_code=http_status.HTTP_403_FORBIDDEN)
    sp = ScheduledPrice.objects.select_for_update().select_related("created_by").filter(id=schedule_id).first()
    if not sp:
        raise ApiException(code=ErrorCode.NOT_FOUND, status_code=http_status.HTTP_404_NOT_FOUND)
    if sp.status != "scheduled":
        raise ApiException(code=ErrorCode.BAD_STATE, message=("التغيير غير معلق", "Schedule is not pending"))
    sp.status = "cancelled"
    sp.save(update_fields=["status"])
    record_audit(
        actor=actor,
        action="price_schedule.cancel",
        entity_type="scheduled_price",
        entity_id=sp.id,
        summary=f"Cancelled scheduled price #{sp.id}",
    )
    return serialize_scheduled_price(sp)


def get_item_price_history(*, actor: User, item_id: int) -> dict[str, Any]:
    if actor.role not in (Role.MANAGER, Role.ACCOUNTANT):
        raise ApiException(code=ErrorCode.FORBIDDEN, status_code=http_status.HTTP_403_FORBIDDEN)
    apply_due_scheduled_prices()
    item = Item.objects.prefetch_related("prices").filter(id=item_id).first()
    if not item:
        raise ApiException(code=ErrorCode.NOT_FOUND, status_code=http_status.HTTP_404_NOT_FOUND)

    changes = list(PriceChange.objects.filter(item_id=item.id).order_by("tier_id", "effective_from", "id"))
    by_tier: dict[int, list[PriceChange]] = {}
    for ch in changes:
        by_tier.setdefault(ch.tier_id, []).append(ch)

    rows: list[dict[str, Any]] = []
    tiers = list(PriceTier.objects.order_by("sort_order", "id"))
    cur_map = {ip.tier_id: ip for ip in item.prices.all()}
    for t in tiers:
        hist = by_tier.get(t.id, [])
        if hist:
            for idx, ch in enumerate(hist):
                row: dict[str, Any] = {
                    "tier_id": t.id,
                    "carton_price": ch.carton_price,
                    "piece_price": ch.piece_price,
                }
                if idx > 0:
                    row["from"] = ch.effective_from.isoformat()
                if idx + 1 < len(hist):
                    row["until"] = hist[idx + 1].effective_from.isoformat()
                rows.append(row)
        elif t.id in cur_map:
            ip = cur_map[t.id]
            rows.append(
                {
                    "tier_id": t.id,
                    "carton_price": ip.carton_price,
                    "piece_price": ip.piece_price,
                }
            )
    rows.reverse()

    scheduled_out: list[dict[str, Any]] = []
    for sp in ScheduledPrice.objects.select_related("created_by").filter(status="scheduled").order_by("start", "id"):
        if any(int(m["id"]) == item.id for m in (sp.items or [])):
            base = serialize_scheduled_price(sp)
            if sp.percent is not None:
                factor = Decimal("1") + (Decimal(sp.percent) / Decimal("100"))
                base["next"] = [
                    {
                        "tier_id": ip.tier_id,
                        "carton_price": max(250, round_250(int((Decimal(ip.carton_price) * factor).quantize(Decimal("1"))))),
                        "piece_price": max(0, round_250(int((Decimal(ip.piece_price) * factor).quantize(Decimal("1"))))),
                    }
                    for ip in sorted(item.prices.all(), key=lambda p: p.tier_id)
                ]
            elif sp.prices:
                base["next"] = list(sp.prices)
            else:
                base["next"] = []
            scheduled_out.append(base)

    return {
        "item_id": item.id,
        "rows": rows,
        "scheduled": scheduled_out,
    }
