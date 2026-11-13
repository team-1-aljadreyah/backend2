"""Warehouse CRUD, zone layout arrangement, and temporary keeper coverage services."""
from __future__ import annotations

import re
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from django.db import transaction
from django.utils import timezone
from rest_framework import status as http_status

from apps.accounts.models import Role, User
from apps.accounts.permissions import works_in_warehouse
from apps.accounts.serializers import serialize_warehouse
from apps.audit.services import record_audit
from apps.catalog.models import Item
from apps.common.exceptions import ApiException, ErrorCode
from apps.inventory.models import Batch
from apps.invoices.models import Invoice
from apps.notifications.services import notify_user
from apps.orders.models import Order, OrderStatus
from apps.receipts.models import Receipt
from apps.stocktakes.models import Stocktake, StocktakeStatus
from apps.supply_orders.models import SupplyOrder, SupplyStatus
from apps.transfers.models import Transfer, TransferStatus
from apps.warehouses.models import Warehouse

_ZONE_CODE_RE = re.compile(r"^[A-Z0-9-]{1,8}$")


def _clean_warehouse_data(data: dict[str, Any], *, warehouse_id: int | None = None) -> dict[str, Any]:
    name = str(data.get("name") or "").strip()
    if not name:
        msg = ("اسم المخزن مطلوب", "Warehouse name is required")
        raise ApiException(code=ErrorCode.VALIDATION, message=msg, errors={"name": msg})
    name_en = str(data.get("name_en") or "").strip() or name

    raw_code = str(data.get("code") or "").strip().upper()
    if raw_code:
        if Warehouse.objects.filter(code=raw_code).exclude(id=warehouse_id).exists():
            msg = ("رمز المخزن مستخدم مسبقاً", "Warehouse code already in use")
            raise ApiException(code=ErrorCode.VALIDATION, message=msg, errors={"code": msg})

    try:
        cap = int(data.get("capacity", 1000))
        if cap < 1 or cap > 10_000_000:
            raise ValueError
    except (TypeError, ValueError) as exc:
        msg = ("سعة المخزن غير صحيحة", "Invalid warehouse capacity")
        raise ApiException(code=ErrorCode.VALIDATION, message=msg, errors={"capacity": msg}) from exc

    def _coord(key: str, low: float, high: float) -> Decimal | None:
        val = data.get(key)
        if val in (None, ""):
            return None
        try:
            d = Decimal(str(val))
            if d < Decimal(str(low)) or d > Decimal(str(high)):
                raise ValueError
            return d
        except (InvalidOperation, ValueError) as exc:
            raise ApiException(
                code=ErrorCode.VALIDATION,
                message=("إحداثيات الموقع خارج النطاق المسموح", "Location coordinates out of Iraq bounds"),
            ) from exc

    lat = _coord("lat", 29.0, 38.0)
    lng = _coord("lng", 38.0, 49.0)
    active = bool(data.get("active", True))
    return {
        "name": name[:120],
        "name_en": name_en[:120],
        "code": raw_code[:32],
        "capacity": cap,
        "lat": lat,
        "lng": lng,
        "active": active,
    }


@transaction.atomic
def create_warehouse(*, actor: User, data: dict[str, Any]) -> dict[str, Any]:
    cleaned = _clean_warehouse_data(data)
    code = cleaned["code"] or f"WH-TMP-{timezone.now().timestamp()}"[:32]
    wh = Warehouse.objects.create(
        name=cleaned["name"],
        name_en=cleaned["name_en"],
        code=code,
        capacity=cleaned["capacity"],
        lat=cleaned["lat"],
        lng=cleaned["lng"],
        active=cleaned["active"],
    )
    if not cleaned["code"]:
        wh.code = f"WH-{wh.id:02d}"
        if Warehouse.objects.filter(code=wh.code).exclude(id=wh.id).exists():
            wh.code = f"WH-{wh.id}"
        wh.save(update_fields=["code"])

    record_audit(
        actor=actor,
        action="create",
        entity_type="settings",
        entity_id=wh.id,
        summary=f"Created warehouse {wh.name_en} ({wh.capacity} cartons)",
        after=serialize_warehouse(wh),
    )
    return serialize_warehouse(wh)


@transaction.atomic
def update_warehouse(*, actor: User, warehouse_id: int, data: dict[str, Any]) -> dict[str, Any]:
    wh = Warehouse.objects.select_for_update().filter(id=warehouse_id).first()
    if not wh:
        raise ApiException(code=ErrorCode.NOT_FOUND, status_code=http_status.HTTP_404_NOT_FOUND)
    before = serialize_warehouse(wh)
    merged = {
        "name": data.get("name", wh.name),
        "name_en": data.get("name_en", wh.name_en if "name" not in data else data.get("name_en") or data.get("name")),
        "code": data.get("code", wh.code),
        "capacity": data.get("capacity", wh.capacity),
        "lat": data.get("lat", wh.lat),
        "lng": data.get("lng", wh.lng),
        "active": data.get("active", wh.active),
    }
    cleaned = _clean_warehouse_data(merged, warehouse_id=wh.id)
    wh.name = cleaned["name"]
    wh.name_en = cleaned["name_en"]
    if cleaned["code"]:
        wh.code = cleaned["code"]
    wh.capacity = cleaned["capacity"]
    wh.lat = cleaned["lat"]
    wh.lng = cleaned["lng"]
    wh.active = cleaned["active"]
    wh.save()
    after = serialize_warehouse(wh)
    record_audit(
        actor=actor,
        action="update",
        entity_type="settings",
        entity_id=wh.id,
        summary=f"Updated warehouse {wh.name_en}",
        before=before,
        after=after,
    )
    return after


@transaction.atomic
def delete_warehouse(*, actor: User, warehouse_id: int) -> None:
    wh = Warehouse.objects.select_for_update().filter(id=warehouse_id).first()
    if not wh:
        raise ApiException(code=ErrorCode.NOT_FOUND, status_code=http_status.HTTP_404_NOT_FOUND)
    if Warehouse.objects.count() <= 1:
        raise ApiException(
            code=ErrorCode.BAD_STATE,
            message=("لا يمكن حذف المخزن الوحيد", "The only warehouse can't be deleted"),
            status_code=http_status.HTTP_409_CONFLICT,
        )

    why: list[str] = []
    goods = Batch.objects.filter(warehouse=wh, qty_left__gt=0).count()
    if goods:
        why.append(f"فيه بضاعة ({goods} دفعة)")

    open_orders = (
        Order.objects.filter(warehouse=wh)
        .exclude(status__in=[OrderStatus.CONFIRMED, OrderStatus.CANCELLED, OrderStatus.EXPIRED])
        .count()
    )
    if open_orders:
        why.append(f"عليه {open_orders} طلب مفتوح")

    moving = Transfer.objects.filter(status=TransferStatus.SENT).filter(
        from_warehouse=wh
    ).count() + Transfer.objects.filter(status=TransferStatus.SENT, to_warehouse=wh).count()
    if moving:
        why.append(f"{moving} تحويل في الطريق")

    supply = SupplyOrder.objects.filter(
        warehouse=wh, status__in=[SupplyStatus.SENT, SupplyStatus.PARTIAL]
    ).count()
    if supply:
        why.append(f"{supply} طلب توريد ينتظر")

    counting = Stocktake.objects.filter(warehouse=wh, status=StocktakeStatus.OPEN).count()
    if counting:
        why.append("جرد مفتوح")

    staff_names = list(
        User.objects.filter(warehouse=wh, active=True, deleted=False).values_list("name", flat=True)
    )
    if staff_names:
        why.append(f"يعمل فيه {', '.join(staff_names)}")

    past = (
        Invoice.objects.filter(warehouse=wh).exists()
        or Order.objects.filter(warehouse=wh).exists()
        or Receipt.objects.filter(warehouse=wh).exists()
        or Transfer.objects.filter(from_warehouse=wh).exists()
        or Transfer.objects.filter(to_warehouse=wh).exists()
        or Batch.objects.filter(warehouse=wh).exists()
    )
    if not why and past:
        why.append("له فواتير وحركات سابقة تبقى في السجلات")

    if why:
        raise ApiException(
            code=ErrorCode.BAD_STATE,
            message=(
                f"لا يمكن حذف {wh.name}: {'؛ '.join(why)}.",
                f"{wh.name_en} can't be deleted: it still has goods, open work, staff or past records.",
            ),
            status_code=http_status.HTTP_409_CONFLICT,
        )

    Stocktake.objects.filter(warehouse=wh).delete()
    SupplyOrder.objects.filter(warehouse=wh).delete()
    name = wh.name
    name_en = wh.name_en
    wid = wh.id
    wh.delete()
    record_audit(
        actor=actor,
        action="cancel",
        entity_type="settings",
        entity_id=wid,
        summary=f"Deleted warehouse {name_en} ({name})",
    )


@transaction.atomic
def save_warehouse_layout(*, actor: User, warehouse_id: int, data: dict[str, Any]) -> dict[str, Any]:
    wh = Warehouse.objects.select_for_update().filter(id=warehouse_id).first()
    if not wh:
        raise ApiException(code=ErrorCode.NOT_FOUND, status_code=http_status.HTTP_404_NOT_FOUND)
    if actor.role == Role.WAREHOUSE and not works_in_warehouse(actor, warehouse_id):
        raise ApiException(
            code=ErrorCode.FORBIDDEN,
            message=("ترتّب مخزنك فقط", "You arrange your own warehouse only"),
            status_code=http_status.HTTP_403_FORBIDDEN,
        )

    raw_zones = data.get("zones") if isinstance(data, dict) else None
    if not isinstance(raw_zones, list) or not raw_zones:
        raise ApiException(
            code=ErrorCode.VALIDATION,
            message=("يحتاج المخزن منطقة واحدة على الأقل", "A warehouse needs at least one zone"),
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
        )

    zones: list[dict[str, str]] = []
    codes: set[str] = set()
    for z in raw_zones:
        if not isinstance(z, dict):
            raise ApiException(code=ErrorCode.VALIDATION, message=("بيانات المنطقة غير صالحة", "Invalid zone"))
        code = str(z.get("code") or "").strip().upper()
        name = str(z.get("name") or "").strip()
        if not name:
            raise ApiException(
                code=ErrorCode.VALIDATION,
                message=("لكل منطقة اسم", "Every zone needs a name"),
                status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            )
        if not _ZONE_CODE_RE.match(code):
            raise ApiException(
                code=ErrorCode.VALIDATION,
                message=("رمز المنطقة حروف وأرقام قصيرة", "A zone code is a few letters and digits"),
                status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            )
        if code in codes:
            raise ApiException(
                code=ErrorCode.VALIDATION,
                message=("رمزان متشابهان لمنطقتين", "Two zones have the same code"),
                status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            )
        codes.add(code)
        zones.append({"code": code, "name": name})

    raw_places = (data.get("places") or {}) if isinstance(data, dict) else {}
    if not isinstance(raw_places, dict):
        raise ApiException(code=ErrorCode.VALIDATION, message=("أماكن المواد غير صالحة", "Invalid item placements"))

    item_ids = {str(iid) for iid in Item.objects.values_list("id", flat=True)}
    places: dict[str, str] = {}
    for raw_item_id, zone_code in raw_places.items():
        if str(raw_item_id) not in item_ids:
            continue
        zc = str(zone_code).strip().upper()
        if zc not in codes:
            raise ApiException(
                code=ErrorCode.VALIDATION,
                message=("مادة في منطقة غير موجودة", "An item is in a zone that doesn't exist"),
                status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            )
        places[str(raw_item_id)] = zc

    wh.layout = {"zones": zones, "places": places}
    wh.save(update_fields=["layout"])
    record_audit(
        actor=actor,
        action="update",
        entity_type="settings",
        entity_id=wh.id,
        summary=f"Arranged {wh.name_en}: {len(zones)} zones",
        after=wh.layout,
    )
    return serialize_warehouse(wh)


@transaction.atomic
def set_warehouse_cover(
    *, actor: User, warehouse_id: int, user_id: int | None, hours: int | None = None
) -> dict[str, Any]:
    wh = Warehouse.objects.select_for_update().filter(id=warehouse_id).first()
    if not wh:
        raise ApiException(code=ErrorCode.NOT_FOUND, status_code=http_status.HTTP_404_NOT_FOUND)

    if user_id is None:
        wh.cover = None
        wh.save(update_fields=["cover"])
        record_audit(
            actor=actor,
            action="update",
            entity_type="settings",
            entity_id=wh.id,
            summary=f"Cleared temporary cover for {wh.name_en}",
        )
        return serialize_warehouse(wh)

    try:
        hrs = int(hours if hours is not None else 24)
        if hrs < 1 or hrs > 72:
            raise ValueError
    except (TypeError, ValueError) as exc:
        raise ApiException(
            code=ErrorCode.VALIDATION,
            message=("عدد الساعات يجب أن يكون بين 1 و 72", "Cover hours must be between 1 and 72"),
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
        ) from exc

    keeper = User.objects.filter(id=user_id, role=Role.WAREHOUSE, active=True, deleted=False).first()
    if not keeper:
        raise ApiException(
            code=ErrorCode.VALIDATION,
            message=("أمين المخزن البديل غير موجود أو غير نشط", "Cover warehouse keeper not found or inactive"),
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
        )
    if keeper.warehouse_id == wh.id:
        raise ApiException(
            code=ErrorCode.BAD_STATE,
            message=("هذا الأمين يعمل أصلاً في نفس المخزن", "This keeper is already assigned to this warehouse"),
            status_code=http_status.HTTP_409_CONFLICT,
        )

    now = timezone.now()
    until = now + timedelta(hours=hrs)
    wh.cover = {
        "user_id": keeper.id,
        "name": keeper.name,
        "until": until.isoformat(),
        "by": actor.name,
        "at": now.isoformat(),
    }
    wh.save(update_fields=["cover"])

    notify_user(
        user_id=keeper.id,
        kind="warehouse_cover",
        title_ar=f"تكليف مؤقت لإدارة {wh.name} لمدة {hrs} ساعة",
        title_en=f"Temporary cover assigned for {wh.name_en} ({hrs}h)",
        ref_type="warehouse",
        ref_id=wh.id,
    )
    record_audit(
        actor=actor,
        action="update",
        entity_type="settings",
        entity_id=wh.id,
        summary=f"Assigned {keeper.name} as temporary cover for {wh.name_en} ({hrs}h)",
        after=wh.cover,
    )
    return serialize_warehouse(wh)
