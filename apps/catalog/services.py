"""Catalog serialization & item/price/image services."""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import uuid4

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from rest_framework import status as http_status

from apps.accounts.models import PriceTier, Role, SiteConfig, User
from apps.accounts.permissions import get_active_customer_id, sees_cost
from apps.accounts.profiles import (
    batch_custom_values,
    custom_values_for_object,
    validate_and_apply_custom_on_create,
)
from apps.audit.services import record_audit
from apps.catalog.models import Category, Item, ItemPrice, PriceChange
from apps.common.exceptions import ApiException, ErrorCode
from apps.common.models import StorageObject
from apps.common.utils import decode_image_data_url
from apps.inventory.services import (
    build_item_stock_and_expiry_maps,
    item_stock_dict,
)
from integrations.storage import get_storage

_WEIGHT_GUESS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(kg|g|l|ml)\b", re.IGNORECASE)


def guess_weight_kg(name_en: str) -> float:
    m = _WEIGHT_GUESS_RE.search(name_en or "")
    if not m:
        return 0.5
    n = float(m.group(1))
    unit = m.group(2).lower()
    kg = n / 1000.0 if unit in ("g", "ml") else n
    return max(0.1, round(kg * 1.05, 2))


def item_piece_weight_kg(item: Item) -> float:
    if item.weight_kg is not None:
        return float(item.weight_kg)
    return guess_weight_kg(item.name_en)


def serialize_item(
    item: Item,
    *,
    actor: User,
    request: Any = None,
    stock_data: dict[str, Any] | None = None,
    nearest_expiry: str | None = None,
    special_price_map: dict[int, tuple[int, int]] | None = None,
    active_tier_id: int | None = None,
    storage: Any = None,
    custom_values: dict[str, Any] | None = None,
    clearance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    prices_qs = sorted(item.prices.all(), key=lambda p: p.tier_id)
    if actor.role == Role.SHOP and active_tier_id is not None:
        special = (special_price_map or {}).get(item.id)
        tier_row = next((p for p in prices_qs if p.tier_id == active_tier_id), None) or (prices_qs[0] if prices_qs else None)
        if special:
            prices_out = [{"tier_id": active_tier_id, "carton_price": special[0], "piece_price": special[1]}]
        elif tier_row:
            prices_out = [{"tier_id": tier_row.tier_id, "carton_price": tier_row.carton_price, "piece_price": tier_row.piece_price}]
        else:
            prices_out = []
    else:
        prices_out = [
            {
                "tier_id": p.tier_id,
                "carton_price": p.carton_price,
                "piece_price": p.piece_price,
            }
            for p in prices_qs
        ]

    photo_url: str | None = None
    if item.image_path:
        st = storage or get_storage()
        photo_url = st.get_url(
            getattr(settings, "SUPABASE_BUCKET_PRODUCT_IMAGES", "product-images"),
            item.image_path,
            request=request,
            private=False,
        )

    pct_dec = item.commission_percent or Decimal("0")
    pct_out: int | float = int(pct_dec) if pct_dec == int(pct_dec) else float(pct_dec)

    out: dict[str, Any] = {
        "id": item.id,
        "name": item.name,
        "name_en": item.name_en,
        "barcode": item.barcode,
        "category": item.category,
        "pieces_per_carton": item.pieces_per_carton,
        "min_stock": item.min_stock,
        "commission_percent": pct_out,
        "active": bool(item.active),
        "prices": prices_out,
        "stock": stock_data or {"total": 0, "by_warehouse": []},
        "nearest_expiry": nearest_expiry,
    }
    if photo_url:
        out["image"] = photo_url
    if item.weight_kg is not None:
        out["weight_kg"] = float(item.weight_kg)
    if item.brand:
        out["brand"] = item.brand
    if item.video:
        out["video"] = item.video
    if item.pack_name:
        out["pack_name"] = item.pack_name
    if item.piece_name:
        out["piece_name"] = item.piece_name
    if item.sells:
        out["sells"] = item.sells
    if item.offer:
        out["offer"] = item.offer
    if item.deleted:
        out["deleted"] = True
    if clearance:
        out["clearance"] = clearance
    if custom_values:
        out["custom"] = custom_values
    if sees_cost(actor):
        out["avg_cost"] = item.avg_cost
    return out


def _shop_context(actor: User) -> tuple[int | None, dict[int, tuple[int, int]]]:
    if actor.role != Role.SHOP:
        return None, {}
    cid = get_active_customer_id(actor)
    if not cid:
        return None, {}
    from apps.customers.models import Customer

    cust = Customer.objects.filter(id=cid).only("id", "tier_id").first()
    if not cust:
        return None, {}
    specials: dict[int, tuple[int, int]] = {}
    try:
        from apps.customers.models import SpecialPrice

        for sp in SpecialPrice.objects.filter(customer_id=cid):
            specials[sp.item_id] = (sp.carton_price, sp.piece_price)
    except Exception:
        pass
    return cust.tier_id, specials


def list_items_for_actor(*, actor: User, request: Any = None) -> list[dict[str, Any]]:
    from apps.catalog.features_services import active_clearance_map, apply_due_scheduled_prices

    apply_due_scheduled_prices()
    phys, res, nearest_map, warehouses = build_item_stock_and_expiry_maps()
    active_tier_id, special_map = _shop_context(actor)
    storage = get_storage()

    qs = Item.objects.prefetch_related("prices").order_by("id")
    if actor.role == Role.SHOP:
        qs = qs.filter(active=True)

    items = list(qs)
    item_ids = [it.id for it in items]
    custom_map = batch_custom_values("item", item_ids, actor)
    clearance_map = active_clearance_map(item_ids)

    return [
        serialize_item(
            it,
            actor=actor,
            request=request,
            stock_data=item_stock_dict(it.id, phys=phys, res=res, warehouses=warehouses),
            nearest_expiry=nearest_map.get(it.id),
            special_price_map=special_map,
            active_tier_id=active_tier_id,
            storage=storage,
            custom_values=custom_map.get(it.id),
            clearance=clearance_map.get(it.id),
        )
        for it in items
    ]


def get_item_for_actor(*, actor: User, item_id: int, request: Any = None) -> dict[str, Any]:
    from apps.catalog.features_services import active_clearance_map, apply_due_scheduled_prices

    apply_due_scheduled_prices()
    item = Item.objects.prefetch_related("prices").filter(id=item_id).first()
    if not item or (actor.role == Role.SHOP and not item.active):
        raise ApiException(code=ErrorCode.NOT_FOUND, status_code=http_status.HTTP_404_NOT_FOUND)
    phys, res, nearest_map, warehouses = build_item_stock_and_expiry_maps()
    active_tier_id, special_map = _shop_context(actor)
    clearance_map = active_clearance_map([item.id])
    return serialize_item(
        item,
        actor=actor,
        request=request,
        stock_data=item_stock_dict(item.id, phys=phys, res=res, warehouses=warehouses),
        nearest_expiry=nearest_map.get(item.id),
        special_price_map=special_map,
        active_tier_id=active_tier_id,
        custom_values=custom_values_for_object("item", item.id, actor),
        clearance=clearance_map.get(item.id),
    )


def _validate_prices_payload(prices: Any) -> list[tuple[PriceTier, int, int]]:
    if not isinstance(prices, list) or not prices:
        msg = ("قائمة الأسعار مطلوبة", "Item tier prices are required")
        raise ApiException(code=ErrorCode.VALIDATION, message=msg, errors={"prices": msg})
    tier_map = {t.id: t for t in PriceTier.objects.all()}
    parsed: list[tuple[PriceTier, int, int]] = []
    seen: set[int] = set()
    for p in prices:
        try:
            tid = int(p["tier_id"])
            cp = int(p["carton_price"])
            pp = int(p["piece_price"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ApiException(code=ErrorCode.VALIDATION, message=("صيغة الأسعار غير صالحة", "Invalid price entry")) from exc
        if tid not in tier_map or tid in seen or cp < 0 or pp < 0:
            raise ApiException(code=ErrorCode.VALIDATION, message=("قيم الأسعار غير صالحة", "Invalid tier or negative price"))
        seen.add(tid)
        parsed.append((tier_map[tid], cp, pp))
    return parsed


def _validate_offer(offer_data: Any, prices: list[tuple[PriceTier, int, int]]) -> dict[str, Any] | None:
    if not offer_data:
        return None
    if not isinstance(offer_data, dict):
        raise ApiException(code=ErrorCode.VALIDATION, message=("عرض الكمية غير صالح", "Invalid offer payload"))
    unit = str(offer_data.get("unit") or "")
    if unit not in ("carton", "piece"):
        raise ApiException(code=ErrorCode.VALIDATION, message=("الوحدة يجب أن تكون كارتون أو قطعة", "Unit must be carton or piece"), errors={"offer.unit": ("كارتون أو قطعة", "Carton or piece")})
    try:
        min_qty = int(offer_data.get("min", 0))
        if min_qty < 2:
            raise ValueError
    except (TypeError, ValueError):
        raise ApiException(code=ErrorCode.VALIDATION, message=("الحد الأدنى من 2 فما فوق", "Min qty must be >= 2"), errors={"offer.min": ("من 2 فما فوق", "From 2 up")})
    try:
        off = int(offer_data.get("off", 0))
        if off <= 0:
            raise ValueError
    except (TypeError, ValueError):
        raise ApiException(code=ErrorCode.VALIDATION, message=("قيمة الخصم يجب أن تكون موجبة", "Offer off must be > 0"), errors={"offer.off": ("قيمة الخصم غير صحيحة", "Invalid discount")})

    unit_prices = [cp if unit == "carton" else pp for _, cp, pp in prices if (cp if unit == "carton" else pp) > 0]
    lowest = min(unit_prices) if unit_prices else 0
    if lowest > 0 and off >= lowest:
        msg = (f"الخصم يجب أن يكون أقل من أقل سعر ({lowest} د.ع)", "Offer off must be less than lowest price")
        raise ApiException(code=ErrorCode.VALIDATION, message=msg, errors={"offer.off": msg})

    until_str = offer_data.get("until")
    if until_str:
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", str(until_str)):
            raise ApiException(code=ErrorCode.VALIDATION, message=("صيغة التاريخ غير صحيحة", "Invalid date format"), errors={"offer.until": ("تاريخ غير صحيح", "Invalid date")})

    clean: dict[str, Any] = {"unit": unit, "min": min_qty, "off": off}
    if until_str:
        clean["until"] = str(until_str)
    return clean



@transaction.atomic
def create_item(*, actor: User, data: dict[str, Any], request: Any = None) -> dict[str, Any]:
    name = str(data.get("name") or "").strip()
    name_en = str(data.get("name_en") or "").strip()
    category = str(data.get("category") or "").strip()
    barcode = str(data.get("barcode") or "").strip()
    errors: dict[str, tuple[str, str]] = {}
    if not name:
        errors["name"] = ("اسم المادة مطلوب", "Arabic name is required")
    if not name_en:
        errors["name_en"] = ("الاسم الإنجليزي مطلوب", "English name is required")
    if not category:
        errors["category"] = ("الصنف مطلوب", "Category is required")
    try:
        ppc = int(data.get("pieces_per_carton") or 0)
        if ppc < 1:
            raise ValueError
    except (TypeError, ValueError):
        errors["pieces_per_carton"] = ("عدد القطع بالكارتون يجب أن يكون 1 على الأقل", "Pieces per carton must be >= 1")
        ppc = 1
    try:
        min_stock = int(data.get("min_stock", 0))
        if min_stock < 0:
            raise ValueError
    except (TypeError, ValueError):
        errors["min_stock"] = ("الحد الأدنى غير صحيح", "Invalid min stock")
        min_stock = 0

    if errors:
        raise ApiException(code=ErrorCode.VALIDATION, message=next(iter(errors.values())), errors=errors)

    try:
        comm_pct = Decimal(str(data.get("commission_percent", 0) or 0))
        if comm_pct < 0 or comm_pct > 100:
            raise ValueError
    except (InvalidOperation, ValueError) as exc:
        msg = ("نسبة العمولة يجب أن تكون بين 0 و 100", "Commission percent must be between 0 and 100")
        raise ApiException(code=ErrorCode.VALIDATION, message=msg, errors={"commission_percent": msg}) from exc

    parsed_prices = _validate_prices_payload(data.get("prices"))
    weight_kg = None
    if data.get("weight_kg") not in (None, ""):
        try:
            w_dec = Decimal(str(data["weight_kg"]))
            if w_dec < Decimal("0.01") or w_dec > Decimal("100.000"):
                raise ValueError
            weight_kg = w_dec
        except (InvalidOperation, ValueError) as exc:
            msg = ("وزن القطعة يجب أن يكون بين 0.01 و 100 كغم", "Item weight_kg must be between 0.01 and 100")
            raise ApiException(code=ErrorCode.VALIDATION, message=msg, errors={"weight_kg": msg}) from exc

    Category.objects.get_or_create(name=category, defaults={"name_en": category, "sort_order": Category.objects.count() + 1})

    brand = str(data.get("brand") or "").strip()
    if brand and len(brand) > 40:
        raise ApiException(code=ErrorCode.VALIDATION, message=("اسم قصير (40 حرفاً على الأكثر)", "A short name (40 letters at most)"), errors={"brand": ("اسم قصير (40 حرفاً على الأكثر)", "A short name (40 letters at most)")})
    pack_name = str(data.get("pack_name") or "").strip()
    if pack_name and len(pack_name) > 20:
        raise ApiException(code=ErrorCode.VALIDATION, message=("اسم قصير (20 حرفاً على الأكثر)", "A short name (20 letters at most)"))
    piece_name = str(data.get("piece_name") or "").strip()
    if piece_name and len(piece_name) > 20:
        raise ApiException(code=ErrorCode.VALIDATION, message=("اسم قصير (20 حرفاً على الأكثر)", "A short name (20 letters at most)"))
    sells = str(data.get("sells") or "both").strip()
    if sells not in ("both", "pack", "piece"):
        sells = "both"

    offer_clean = None
    if "offer" in data and data["offer"]:
        offer_clean = _validate_offer(data["offer"], parsed_prices)

    item = Item.objects.create(
        name=name,
        name_en=name_en,
        barcode=barcode,
        category=category,
        pieces_per_carton=ppc,
        min_stock=min_stock,
        commission_percent=comm_pct,
        weight_kg=weight_kg,
        brand=brand,
        pack_name=pack_name,
        piece_name=piece_name,
        sells=sells,
        offer=offer_clean,
        active=bool(data.get("active", True)),
    )
    validate_and_apply_custom_on_create("item", item.id, actor, data.get("custom"))
    now = timezone.now()
    for tier, cp, pp in parsed_prices:
        ItemPrice.objects.create(item=item, tier=tier, carton_price=cp, piece_price=pp)
        PriceChange.objects.create(item=item, tier=tier, carton_price=cp, piece_price=pp, changed_by=actor, effective_from=now)

    record_audit(
        actor=actor,
        action="item.create",
        entity_type="item",
        entity_id=item.id,
        summary=f"Created item {item.name_en}",
        after={"id": item.id, "name": item.name, "category": item.category},
    )
    return get_item_for_actor(actor=actor, item_id=item.id, request=request)


@transaction.atomic
def update_item(*, actor: User, item_id: int, data: dict[str, Any], request: Any = None) -> dict[str, Any]:
    item = Item.objects.select_for_update().filter(id=item_id).first()
    if not item:
        raise ApiException(code=ErrorCode.NOT_FOUND, status_code=http_status.HTTP_404_NOT_FOUND)

    before = {"name": item.name, "name_en": item.name_en, "category": item.category, "active": item.active}
    if "name" in data:
        val = str(data.get("name") or "").strip()
        if not val:
            raise ApiException(code=ErrorCode.VALIDATION, message=("الاسم مطلوب", "Name is required"), errors={"name": ("الاسم مطلوب", "Name is required")})
        item.name = val
    if "name_en" in data:
        val = str(data.get("name_en") or "").strip()
        if not val:
            raise ApiException(code=ErrorCode.VALIDATION, message=("الاسم الإنجليزي مطلوب", "English name is required"))
        item.name_en = val
    if "barcode" in data:
        item.barcode = str(data.get("barcode") or "").strip()
    if "category" in data:
        cat = str(data.get("category") or "").strip()
        if not cat:
            raise ApiException(code=ErrorCode.VALIDATION, message=("الصنف مطلوب", "Category is required"))
        Category.objects.get_or_create(name=cat, defaults={"name_en": cat, "sort_order": Category.objects.count() + 1})
        item.category = cat
    if "pieces_per_carton" in data:
        try:
            ppc = int(data["pieces_per_carton"])
            if ppc < 1:
                raise ValueError
        except (TypeError, ValueError) as exc:
            raise ApiException(code=ErrorCode.VALIDATION, message=("عدد القطع غير صحيح", "Invalid pieces_per_carton")) from exc
        item.pieces_per_carton = ppc
    if "min_stock" in data:
        try:
            ms = int(data["min_stock"])
            if ms < 0:
                raise ValueError
        except (TypeError, ValueError) as exc:
            raise ApiException(code=ErrorCode.VALIDATION, message=("الحد الأدنى غير صحيح", "Invalid min_stock")) from exc
        item.min_stock = ms
    if "commission_percent" in data:
        try:
            comm_pct = Decimal(str(data["commission_percent"] or 0))
            if comm_pct < 0 or comm_pct > 100:
                raise ValueError
        except (InvalidOperation, ValueError) as exc:
            msg = ("نسبة العمولة يجب أن تكون بين 0 و 100", "Commission percent must be between 0 and 100")
            raise ApiException(code=ErrorCode.VALIDATION, message=msg, errors={"commission_percent": msg}) from exc
        item.commission_percent = comm_pct
    if "active" in data:
        item.active = bool(data["active"])
    if "brand" in data:
        val = str(data.get("brand") or "").strip()
        if val and len(val) > 40:
            raise ApiException(code=ErrorCode.VALIDATION, message=("اسم قصير (40 حرفاً على الأكثر)", "A short name (40 letters at most)"), errors={"brand": ("اسم قصير (40 حرفاً على الأكثر)", "A short name (40 letters at most)")})
        item.brand = val
    if "pack_name" in data:
        val = str(data.get("pack_name") or "").strip()
        if val and len(val) > 20:
            raise ApiException(code=ErrorCode.VALIDATION, message=("اسم قصير (20 حرفاً على الأكثر)", "A short name (20 letters at most)"))
        item.pack_name = val
    if "piece_name" in data:
        val = str(data.get("piece_name") or "").strip()
        if val and len(val) > 20:
            raise ApiException(code=ErrorCode.VALIDATION, message=("اسم قصير (20 حرفاً على الأكثر)", "A short name (20 letters at most)"))
        item.piece_name = val
    if "sells" in data:
        val = str(data.get("sells") or "both").strip()
        if val in ("both", "pack", "piece"):
            item.sells = val
    if "offer" in data:
        off_val = data.get("offer")
        if off_val is None:
            item.offer = None
        else:
            current_prices = [(p.tier, p.carton_price, p.piece_price) for p in item.prices.all()]
            item.offer = _validate_offer(off_val, current_prices)
    if "weight_kg" in data:
        if data["weight_kg"] in (None, ""):
            item.weight_kg = None
        else:
            try:
                w_dec = Decimal(str(data["weight_kg"]))
                if w_dec < Decimal("0.01") or w_dec > Decimal("100.000"):
                    raise ValueError
                item.weight_kg = w_dec
            except (InvalidOperation, ValueError) as exc:
                msg = ("وزن القطعة يجب أن يكون بين 0.01 و 100 كغم", "Item weight_kg must be between 0.01 and 100")
                raise ApiException(code=ErrorCode.VALIDATION, message=msg, errors={"weight_kg": msg}) from exc

    item.save()

    if "prices" in data and data["prices"] is not None:
        parsed_prices = _validate_prices_payload(data["prices"])
        now = timezone.now()
        for tier, cp, pp in parsed_prices:
            existing = ItemPrice.objects.filter(item=item, tier=tier).first()
            changed = (existing is None) or (existing.carton_price != cp) or (existing.piece_price != pp)
            ItemPrice.objects.update_or_create(
                item=item,
                tier=tier,
                defaults={"carton_price": cp, "piece_price": pp},
            )
            if changed:
                PriceChange.objects.create(
                    item=item,
                    tier=tier,
                    carton_price=cp,
                    piece_price=pp,
                    changed_by=actor,
                    effective_from=now,
                )

    record_audit(
        actor=actor,
        action="item.update",
        entity_type="item",
        entity_id=item.id,
        summary=f"Updated item {item.name_en}",
        before=before,
        after={"name": item.name, "name_en": item.name_en, "category": item.category, "active": item.active},
    )
    return get_item_for_actor(actor=actor, item_id=item.id, request=request)


@transaction.atomic
def update_item_image(*, actor: User, item_id: int, image_data_url: str | None, request: Any = None) -> dict[str, Any]:
    item = Item.objects.select_for_update().filter(id=item_id).first()
    if not item:
        raise ApiException(code=ErrorCode.NOT_FOUND, status_code=http_status.HTTP_404_NOT_FOUND)

    storage = get_storage()
    bucket = getattr(settings, "SUPABASE_BUCKET_PRODUCT_IMAGES", "product-images")
    old_path = item.image_path

    if image_data_url is None or image_data_url == "":
        item.image_path = ""
        item.save(update_fields=["image_path"])
        if old_path:
            try:
                storage.delete(bucket, old_path)
            except Exception:
                pass
    else:
        raw, ext, mime = decode_image_data_url(image_data_url, max_bytes=5 * 1024 * 1024, max_dim=1200)
        new_key = f"items/{item.id}/{uuid4().hex[:12]}.{ext}"
        storage.put_bytes(bucket, new_key, raw, mime)
        StorageObject.objects.create(
            bucket=bucket,
            object_path=new_key,
            mime_type=mime,
            byte_size=len(raw),
            uploaded_by=actor,
        )
        item.image_path = new_key
        item.save(update_fields=["image_path"])
        if old_path and old_path != new_key:
            try:
                storage.delete(bucket, old_path)
            except Exception:
                pass

    record_audit(
        actor=actor,
        action="item.image",
        entity_type="item",
        entity_id=item.id,
        summary="Updated item photo" if item.image_path else "Removed item photo",
    )
    return get_item_for_actor(actor=actor, item_id=item.id, request=request)


@transaction.atomic
def quick_update_item(*, actor: User, item_id: int, data: dict[str, Any], request: Any = None) -> dict[str, Any]:
    item = Item.objects.select_for_update().filter(id=item_id).first()
    if not item:
        raise ApiException(code=ErrorCode.NOT_FOUND, status_code=http_status.HTTP_404_NOT_FOUND)
    if not isinstance(data, dict):
        raise ApiException(code=ErrorCode.VALIDATION, message=("البيانات غير صالحة", "Invalid payload"))

    before = {"name": item.name, "name_en": item.name_en}
    changed = False
    if "name" in data:
        val = str(data.get("name") or "").strip()
        if not val:
            msg = ("اسم المادة مطلوب", "Arabic name is required")
            raise ApiException(code=ErrorCode.VALIDATION, message=msg, errors={"name": msg})
        item.name = val
        changed = True
    if "name_en" in data:
        val = str(data.get("name_en") or "").strip()
        if not val:
            msg = ("الاسم الإنجليزي مطلوب", "English name is required")
            raise ApiException(code=ErrorCode.VALIDATION, message=msg, errors={"name_en": msg})
        item.name_en = val
        changed = True
    if changed:
        item.save(update_fields=["name", "name_en", "updated_at"])

    tier_id = data.get("tier_id")
    if tier_id is not None and ("carton_price" in data or "piece_price" in data):
        tier = PriceTier.objects.filter(id=int(tier_id)).first()
        if not tier:
            raise ApiException(code=ErrorCode.VALIDATION, message=("فئة السعر غير موجودة", "Price tier not found"))
        existing = ItemPrice.objects.filter(item=item, tier=tier).first()
        cp = existing.carton_price if existing else 0
        pp = existing.piece_price if existing else 0
        try:
            if "carton_price" in data and data["carton_price"] is not None:
                cp = int(data["carton_price"])
            if "piece_price" in data and data["piece_price"] is not None:
                pp = int(data["piece_price"])
            if cp < 0 or pp < 0:
                raise ValueError
        except (TypeError, ValueError) as exc:
            raise ApiException(code=ErrorCode.VALIDATION, message=("الأسعار غير صالحة", "Prices cannot be negative")) from exc

        ItemPrice.objects.update_or_create(
            item=item,
            tier=tier,
            defaults={"carton_price": cp, "piece_price": pp},
        )
        if not existing or existing.carton_price != cp or existing.piece_price != pp:
            PriceChange.objects.create(
                item=item,
                tier=tier,
                carton_price=cp,
                piece_price=pp,
                changed_by=actor,
                effective_from=timezone.now(),
            )

    record_audit(
        actor=actor,
        action="update",
        entity_type="item",
        entity_id=item.id,
        summary=f"Quick edited {item.name_en}",
        before=before,
        after={"name": item.name, "name_en": item.name_en},
    )
    return get_item_for_actor(actor=actor, item_id=item.id, request=request)


@transaction.atomic
def rename_category(*, actor: User, old_name: str, new_name: str) -> dict[str, Any]:
    old_clean = str(old_name or "").strip()
    new_clean = str(new_name or "").strip()
    if not old_clean or not new_clean:
        raise ApiException(
            code=ErrorCode.VALIDATION,
            message=("اسم الصنف القديم والجديد مطلوبان", "Old and new category names are required"),
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
        )
    if old_clean == new_clean:
        return {"old": old_clean, "next": new_clean, "updated_items": 0}

    updated_items = Item.objects.filter(category=old_clean).update(category=new_clean)
    old_cat = Category.objects.filter(name=old_clean).first()
    if old_cat:
        if Category.objects.filter(name=new_clean).exclude(id=old_cat.id).exists():
            old_cat.delete()
        else:
            old_cat.name = new_clean
            if old_cat.name_en == old_clean:
                old_cat.name_en = new_clean
            old_cat.save(update_fields=["name", "name_en"])
    elif updated_items:
        Category.objects.get_or_create(name=new_clean, defaults={"name_en": new_clean, "sort_order": Category.objects.count() + 1})

    for rep in User.objects.filter(role=Role.REP):
        cats = list(rep.rep_categories or [])
        if old_clean in cats:
            rep.rep_categories = [new_clean if c == old_clean else c for c in cats]
            rep.save(update_fields=["rep_categories", "updated_at"])

    cfg = SiteConfig.objects.filter(pk=1).first()
    if cfg:
        cfg_cats = list(cfg.categories or []) if isinstance(cfg.categories, list) else []
        if old_clean in cfg_cats:
            cfg.categories = [new_clean if c == old_clean else c for c in cfg_cats]
            cfg.save(update_fields=["categories", "updated_at"])

    record_audit(
        actor=actor,
        action="update",
        entity_type="settings",
        entity_id=0,
        summary=f"Renamed category {old_clean} -> {new_clean}",
    )
    return {"old": old_clean, "next": new_clean, "updated_items": updated_items}


def get_catalog(*, request: Any = None) -> list[dict[str, Any]]:
    """Public catalog listing active, non-deleted items without prices or internal costs."""
    items = Item.objects.filter(active=True, deleted=False).order_by("category", "id")
    phys, res, _, _ = build_item_stock_and_expiry_maps()
    storage = get_storage()
    bucket = getattr(settings, "SUPABASE_BUCKET_PRODUCT_IMAGES", "product-images")
    result: list[dict[str, Any]] = []

    for it in items:
        wh_map = phys.get(it.id, {})
        res_map = res.get(it.id, {})
        all_wh_ids = set(wh_map.keys()) | set(res_map.keys())
        total_sellable = sum(max(0, wh_map.get(wid, 0) - res_map.get(wid, 0)) for wid in all_wh_ids)

        photo_url = None
        if it.image_path:
            try:
                photo_url = storage.get_url(bucket, it.image_path, request=request, private=False)
            except Exception:
                photo_url = None

        row: dict[str, Any] = {
            "id": it.id,
            "name": it.name,
            "name_en": it.name_en,
            "category": it.category,
            "pieces_per_carton": it.pieces_per_carton,
            "in_stock": total_sellable > 0,
        }
        if it.brand:
            row["brand"] = it.brand
        if photo_url:
            row["image"] = photo_url
        if it.video:
            row["video"] = it.video
        if it.pack_name:
            row["pack_name"] = it.pack_name
        if it.piece_name:
            row["piece_name"] = it.piece_name
        if it.sells:
            row["sells"] = it.sells
        result.append(row)

    return result


@transaction.atomic
def delete_item(*, actor: User, item_id: int) -> dict[str, str]:
    """Manager-only delete: permanently remove if never used; soft-delete (hide) if used before."""
    if actor.role != Role.MANAGER:
        raise ApiException(
            code=ErrorCode.FORBIDDEN,
            status_code=http_status.HTTP_403_FORBIDDEN,
            message=("المدير فقط", "Manager only"),
        )
    item = Item.objects.select_for_update().filter(id=item_id, deleted=False).first()
    if not item:
        raise ApiException(code=ErrorCode.NOT_FOUND, status_code=http_status.HTTP_404_NOT_FOUND)

    # 1. Stock check: any batch with qty_left > 0
    from apps.inventory.models import Batch
    total_left = sum(b.qty_left for b in Batch.objects.filter(item=item))
    if total_left > 0:
        unit_label = item.piece_name or "قطعة"
        msg = (
            f"في المخازن منها {total_left} {unit_label}: أخرجها أولاً (بالجرد أو كتالف) ثم احذفها، أو أوقفها بدل الحذف",
            f"{total_left} pieces are still in stock: take them out first (count or damaged), or stop the item instead",
        )
        raise ApiException(code=ErrorCode.BAD_STATE, status_code=http_status.HTTP_409_CONFLICT, message=msg)

    # 2. Open orders check: pending, needs_approval, approved
    from apps.orders.models import Order
    open_order = Order.objects.filter(
        status__in=["pending", "needs_approval", "approved"],
        lines__item=item,
    ).first()
    if open_order:
        msg = (
            f"عليها الطلب {open_order.number} ولم يُسلَّم بعد: سلّمه أو ألغه أولاً",
            f"Order {open_order.number} with it isn't delivered yet: deliver or cancel it first",
        )
        raise ApiException(code=ErrorCode.BAD_STATE, status_code=http_status.HTTP_409_CONFLICT, message=msg)

    # 3. Open supply orders check: sent, partial
    from apps.supply_orders.models import SupplyOrder
    open_supply = SupplyOrder.objects.filter(
        status__in=["sent", "partial"],
        lines__item=item,
    ).first()
    if open_supply:
        msg = (
            f"عليها طلب التوريد {open_supply.number} ولم يُستلم بعد: استلمه أو ألغه أولاً",
            f"Supply order {open_supply.number} with it isn't received yet",
        )
        raise ApiException(code=ErrorCode.BAD_STATE, status_code=http_status.HTTP_409_CONFLICT, message=msg)

    # 4. Check whether item was ever referenced in historical records
    from apps.invoices.models import InvoiceLine
    from apps.orders.models import OrderLine
    from apps.receipts.models import ReceiptLine
    from apps.stocktakes.models import StocktakeLine
    from apps.transfers.models import TransferLine

    used = (
        Batch.objects.filter(item=item).exists()
        or OrderLine.objects.filter(item=item).exists()
        or InvoiceLine.objects.filter(item=item).exists()
        or ReceiptLine.objects.filter(item=item).exists()
        or TransferLine.objects.filter(item=item).exists()
        or StocktakeLine.objects.filter(item=item).exists()
        or SupplyOrder.objects.filter(lines__item=item).exists()
    )

    from apps.customers.models import SpecialPrice
    SpecialPrice.objects.filter(item=item).delete()

    if not used:
        # Never used: permanent removal
        item.delete()
        record_audit(
            actor=actor,
            action="item.delete",
            entity_type="item",
            entity_id=item_id,
            summary=f"Deleted item {item.name_en} (never used)",
        )
        return {"result": "removed"}

    # Used before: soft delete and hide
    item.deleted = True
    item.active = False
    item.save(update_fields=["deleted", "active", "updated_at"])
    record_audit(
        actor=actor,
        action="item.delete",
        entity_type="item",
        entity_id=item.id,
        summary=f"Deleted item {item.name_en} (hidden; old records keep it)",
    )
    return {"result": "hidden"}


@transaction.atomic
def set_item_video(
    *,
    actor: User,
    item_id: int,
    video_value: str | None = None,
    uploaded_file: Any = None,
    request: Any = None,
) -> dict[str, Any]:
    """Update or remove item video link / uploaded video file."""
    if actor.role not in (Role.MANAGER, Role.WAREHOUSE):
        raise ApiException(
            code=ErrorCode.FORBIDDEN,
            status_code=http_status.HTTP_403_FORBIDDEN,
            message=("غير مصرح", "Forbidden"),
        )
    item = Item.objects.select_for_update().filter(id=item_id).first()
    if not item:
        raise ApiException(code=ErrorCode.NOT_FOUND, status_code=http_status.HTTP_404_NOT_FOUND)

    if uploaded_file is not None:
        size = getattr(uploaded_file, "size", 0)
        if size > 20 * 1024 * 1024:
            raise ApiException(
                code=ErrorCode.VALIDATION,
                message=("حجم الفيديو يجب ألا يتجاوز 20 ميغابايت", "Video must be <= 20MB"),
                errors={"video": ("أكبر من 20MB", "Max 20MB")},
            )
        file_name = getattr(uploaded_file, "name", "video.mp4")
        ext = file_name.split(".")[-1].lower() if "." in file_name else "mp4"
        if ext not in ("mp4", "webm", "mov"):
            raise ApiException(
                code=ErrorCode.VALIDATION,
                message=("صيغة الفيديو غير مدعومة (mp4, webm, mov)", "Supported formats: mp4, webm, mov"),
            )
        mime = f"video/{ext if ext != 'mov' else 'quicktime'}"
        storage = get_storage()
        bucket = getattr(settings, "SUPABASE_BUCKET_PRODUCT_IMAGES", "product-images")
        new_key = f"items/{item.id}/videos/{uuid4().hex[:12]}.{ext}"
        content = uploaded_file.read()
        storage.put_bytes(bucket, new_key, content, mime)
        StorageObject.objects.create(
            bucket=bucket,
            object_path=new_key,
            mime_type=mime,
            byte_size=len(content),
            uploaded_by=actor,
        )
        video_url = storage.get_url(bucket, new_key, request=request, private=False)
        item.video = video_url
        item.save(update_fields=["video", "updated_at"])
    elif video_value is not None:
        val = str(video_value).strip()
        if val == "":
            item.video = ""
        else:
            if not (re.match(r"^https://\S+$", val) or re.match(r"^media:[0-9a-f-]{36}$", val)) or len(val) > 500:
                raise ApiException(
                    code=ErrorCode.VALIDATION,
                    message=("رابط الفيديو غير صحيح", "The video link isn't valid"),
                    errors={"video": ("رابط غير صحيح", "Invalid link")},
                )
            item.video = val
        item.save(update_fields=["video", "updated_at"])
    else:
        item.video = ""
        item.save(update_fields=["video", "updated_at"])

    record_audit(
        actor=actor,
        action="item.video",
        entity_type="item",
        entity_id=item.id,
        summary=f"Video for {item.name_en}" if item.video else f"Removed video for {item.name_en}",
    )
    return get_item_for_actor(actor=actor, item_id=item.id, request=request)


