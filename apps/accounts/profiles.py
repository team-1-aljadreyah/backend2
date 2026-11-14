"""White-label site configuration, permission profiles, and custom fields service."""
from __future__ import annotations

import copy
import re
from datetime import datetime
from typing import Any

from django.db import transaction
from rest_framework import status as http_status

from apps.accounts.models import CustomFieldValue, Role, SiteConfig, User
from apps.audit.services import record_audit
from apps.common.exceptions import ApiException, ErrorCode
from apps.common.utils import baghdad_now, decode_image_data_url
from integrations.storage import get_storage

DEFAULT_SITE: dict[str, Any] = {
    "brand": {
        "name": "النهرين",
        "name_en": "Al-Nahrain",
        "tagline": "للمواد الغذائية بالجملة",
        "tagline_en": "Wholesale foods",
        "company": "شركة النهرين للمواد الغذائية",
        "company_en": "Al-Nahrain Foods Co.",
        "phone": "",
    },
    "nav": {"order": [], "hidden": [], "groups": {}, "labels": {}, "group_labels": {}},
    "dashboard": {"order": [], "hidden": [], "labels": {}},
    "categories": [],
    "texts": {},
    "layouts": {},
    "look": {},
    "profiles": [],
    "apps": [],
    "fields": [],
    "rules": [],
    "banners": [],
}

RULE_FIELDS: dict[str, dict[str, dict[str, Any]]] = {
    "order_created": {
        "total": {"ar": "مبلغ الطلب", "en": "Order total", "type": "number"},
        "discount": {"ar": "نسبة الخصم %", "en": "Discount %", "type": "number"},
        "lines": {"ar": "عدد المواد", "en": "Number of items", "type": "number"},
        "by_role": {"ar": "من كتبه", "en": "Written by", "type": "text"},
        "area": {"ar": "منطقة الزبون", "en": "Customer's area", "type": "text"},
    },
    "payment_created": {
        "amount": {"ar": "مبلغ التسديد", "en": "Amount", "type": "number"},
        "method": {"ar": "طريقة الدفع", "en": "Method", "type": "text"},
    },
    "expense_created": {
        "amount": {"ar": "مبلغ الصرف", "en": "Amount", "type": "number"},
        "category": {"ar": "البند", "en": "Category", "type": "text"},
        "method": {"ar": "طريقة الدفع", "en": "Method", "type": "text"},
    },
    "return_created": {
        "amount": {"ar": "مبلغ المرتجع", "en": "Amount", "type": "number"},
    },
}

_VALID_PROFILE_ROLES = frozenset({"warehouse", "staff", "rep", "accountant", "shop"})
_VALID_OPS = frozenset({"gt", "gte", "lt", "eq", "neq"})
_VALID_CUSTOM_OBJECTS = frozenset({"customer", "item", "order"})
_VALID_CUSTOM_TYPES = frozenset({"text", "number", "date", "list", "yesno"})
_HEX_COLOR_RE = re.compile(r"^#[0-9a-f]{6}$", re.IGNORECASE)
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_SELF_WRITE_PREFIXES = (
    "/api/v1/auth/",
    "/api/v1/me/shop",
    "/api/v1/me/photo",
    "/api/v1/me/password",
    "/api/v1/me/shops/",
    "/api/v1/me/delete",
    "/api/v1/notifications/",
    "/api/v1/chat/",
    "/api/v1/realtime/ticket",
)

# Maps frontend page paths in `profile.denied` to restricted API prefixes.
_DENIED_PAGE_API_MAP: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("/orders", ("/api/v1/orders",)),
    ("/customers", ("/api/v1/customers",)),
    ("/items", ("/api/v1/items", "/api/v1/categories", "/api/v1/price-tiers")),
    ("/invoices", ("/api/v1/invoices",)),
    ("/payments", ("/api/v1/payments",)),
    ("/expenses", ("/api/v1/expenses",)),
    ("/inventory", ("/api/v1/inventory",)),
    ("/transfers", ("/api/v1/transfers",)),
    ("/receipts", ("/api/v1/receipts",)),
    ("/stocktakes", ("/api/v1/stocktakes",)),
    ("/supply", ("/api/v1/supply-orders",)),
    ("/returns", ("/api/v1/returns",)),
    ("/reports", ("/api/v1/reports", "/api/v1/dashboard")),
    ("/commissions", ("/api/v1/commissions", "/api/v1/reps/targets")),
    ("/audit", ("/api/v1/audit",)),
    ("/chat", ("/api/v1/chat",)),
    ("/settings", ("/api/v1/settings", "/api/v1/setup")),
    ("/users", ("/api/v1/users",)),
)


def _normalize_profile(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    item = copy.deepcopy(raw)
    item.pop("can", None)
    item["denied"] = list(item.get("denied")) if isinstance(item.get("denied"), list) else []
    item["limits"] = dict(item.get("limits")) if isinstance(item.get("limits"), dict) else {}
    if "deny_actions" in item and not isinstance(item["deny_actions"], list):
        item.pop("deny_actions", None)
    return item


def _normalize_app(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    item = copy.deepcopy(raw)
    item["color"] = str(item.get("color") or "#e1692b")
    item["icon"] = str(item.get("icon") or "grid")
    item["pages"] = list(item.get("pages")) if isinstance(item.get("pages"), list) else []
    item["roles"] = list(item.get("roles")) if isinstance(item.get("roles"), list) else []
    item["profiles"] = list(item.get("profiles")) if isinstance(item.get("profiles"), list) else []
    item["active"] = bool(item.get("active", True))
    return item


def _normalize_field(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    item = copy.deepcopy(raw)
    item["view_roles"] = list(item.get("view_roles")) if isinstance(item.get("view_roles"), list) else []
    item["edit_roles"] = list(item.get("edit_roles")) if isinstance(item.get("edit_roles"), list) else []
    item["active"] = bool(item.get("active", True))
    item["required"] = bool(item.get("required", False))
    if "in_table" in item:
        item["in_table"] = bool(item["in_table"])
    if item.get("type") == "list":
        item["options"] = list(item.get("options")) if isinstance(item.get("options"), list) else []
    return item


def _normalize_rule(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    item = copy.deepcopy(raw)
    item["active"] = bool(item.get("active", True))
    item["conditions"] = list(item.get("conditions")) if isinstance(item.get("conditions"), list) else []
    item["notify"] = list(item.get("notify")) if isinstance(item.get("notify"), list) else []
    item["approval"] = bool(item.get("approval", False))
    return item


def serialize_site(cfg: SiteConfig, *, request: Any = None) -> dict[str, Any]:
    brand = copy.deepcopy(DEFAULT_SITE["brand"])
    if isinstance(cfg.brand_extra, dict):
        brand.update(cfg.brand_extra)
    if cfg.brand_name:
        brand["name"] = cfg.brand_name
    if cfg.brand_name_en:
        brand["name_en"] = cfg.brand_name_en
    if cfg.brand_tagline:
        brand["tagline"] = cfg.brand_tagline
    if cfg.brand_tagline_en:
        brand["tagline_en"] = cfg.brand_tagline_en
    if cfg.brand_logo_path:
        brand["logo"] = get_storage().get_url("product-images", cfg.brand_logo_path, request=request)
    if cfg.brand_color:
        brand["color"] = cfg.brand_color

    nav = copy.deepcopy(DEFAULT_SITE["nav"])
    if isinstance(cfg.nav, dict):
        nav.update(cfg.nav)

    dashboard = copy.deepcopy(DEFAULT_SITE["dashboard"])
    if isinstance(cfg.dashboard, dict):
        dashboard.update(cfg.dashboard)

    profiles = [p for raw in (cfg.profiles or []) if (p := _normalize_profile(raw)) is not None] if isinstance(cfg.profiles, list) else []
    apps = [a for raw in (cfg.apps or []) if (a := _normalize_app(raw)) is not None] if isinstance(cfg.apps, list) else []
    fields = [f for raw in (cfg.fields or []) if (f := _normalize_field(raw)) is not None] if isinstance(cfg.fields, list) else []
    rules = [r for raw in (cfg.rules or []) if (r := _normalize_rule(raw)) is not None] if isinstance(cfg.rules, list) else []

    return {
        "brand": brand,
        "nav": nav,
        "dashboard": dashboard,
        "categories": list(cfg.categories) if isinstance(cfg.categories, list) else [],
        "texts": dict(cfg.texts) if isinstance(cfg.texts, dict) else {},
        "layouts": dict(cfg.layouts) if isinstance(cfg.layouts, dict) else {},
        "look": dict(cfg.look) if isinstance(cfg.look, dict) else {},
        "profiles": profiles,
        "apps": apps,
        "fields": fields,
        "rules": rules,
        "banners": list(cfg.banners) if isinstance(cfg.banners, list) else [],
    }


def get_site_payload(request: Any = None, *, include_advanced: bool = True) -> dict[str, Any]:
    from apps.accounts.business_rules import public_rules

    data = serialize_site(SiteConfig.get_solo(), request=request)
    if not include_advanced:
        data["profiles"] = []
        data["apps"] = []
        data["fields"] = []
        data["rules"] = []
    data["live"] = public_rules()
    return data


def _validate_advanced_site(next_site: dict[str, Any]) -> None:
    profile_ids: set[str] = set()
    for p in next_site["profiles"]:
        if not isinstance(p, dict):
            raise ApiException(code=ErrorCode.VALIDATION, message=("بيانات الملف غير صحيحة", "Invalid profile entry"))
        pid = str(p.get("id") or "").strip()
        if not pid or pid in profile_ids:
            raise ApiException(code=ErrorCode.VALIDATION, message=("ملف مكرر", "Duplicate profile"))
        profile_ids.add(pid)
        if not str(p.get("name") or "").strip():
            raise ApiException(code=ErrorCode.VALIDATION, message=("اسم ملف الصلاحيات مطلوب", "The profile needs a name"))
        if p.get("role") not in _VALID_PROFILE_ROLES:
            raise ApiException(code=ErrorCode.VALIDATION, message=("اختر وظيفة الملف", "Choose the profile's job"))
        denied = p.get("denied")
        if not isinstance(denied, list) or any(not isinstance(h, str) or not h.startswith("/") for h in denied):
            raise ApiException(code=ErrorCode.VALIDATION, message=("صفحات غير صحيحة", "Wrong pages"))

    app_ids: set[str] = set()
    for app_def in next_site["apps"]:
        if not isinstance(app_def, dict):
            raise ApiException(code=ErrorCode.VALIDATION, message=("بيانات التطبيق غير صحيحة", "Invalid app entry"))
        aid = str(app_def.get("id") or "").strip()
        if not aid or aid in app_ids:
            raise ApiException(code=ErrorCode.VALIDATION, message=("تطبيق مكرر", "Duplicate app"))
        app_ids.add(aid)
        if not str(app_def.get("name") or "").strip():
            raise ApiException(code=ErrorCode.VALIDATION, message=("اسم التطبيق مطلوب", "The app needs a name"))
        if not _HEX_COLOR_RE.match(str(app_def.get("color") or "")):
            raise ApiException(code=ErrorCode.VALIDATION, message=("لون التطبيق غير صحيح", "The app's color isn't valid"))
        pages = app_def.get("pages") or []
        if not isinstance(pages, list) or not pages:
            raise ApiException(
                code=ErrorCode.VALIDATION,
                message=(f"اختر صفحات «{app_def.get('name')}»", f"Choose the pages of {app_def.get('name')}"),
            )
        if any(not isinstance(h, str) or not h.startswith("/") for h in pages):
            raise ApiException(code=ErrorCode.VALIDATION, message=("صفحات غير صحيحة", "Wrong pages"))

    for p in next_site["profiles"]:
        if p.get("default_app") and p["default_app"] not in app_ids:
            p.pop("default_app", None)

    rule_ids: set[str] = set()
    for r in next_site["rules"]:
        if not isinstance(r, dict):
            raise ApiException(code=ErrorCode.VALIDATION, message=("بيانات القاعدة غير صحيحة", "Invalid rule entry"))
        rid = str(r.get("id") or "").strip()
        if not rid or rid in rule_ids:
            raise ApiException(code=ErrorCode.VALIDATION, message=("قاعدة مكررة", "Duplicate rule"))
        rule_ids.add(rid)
        rname = str(r.get("name") or "").strip()
        if not rname:
            raise ApiException(code=ErrorCode.VALIDATION, message=("اسم القاعدة مطلوب", "The rule needs a name"))
        offered = RULE_FIELDS.get(str(r.get("trigger") or ""))
        if offered is None:
            raise ApiException(code=ErrorCode.VALIDATION, message=("اختر متى تعمل القاعدة", "Choose when the rule runs"))
        for c in r.get("conditions") or []:
            if not isinstance(c, dict):
                raise ApiException(code=ErrorCode.VALIDATION, message=(f"شرط غير صحيح في «{rname}»", f"A wrong condition in {rname}"))
            f = offered.get(str(c.get("field") or ""))
            if not f or c.get("op") not in _VALID_OPS:
                raise ApiException(code=ErrorCode.VALIDATION, message=(f"شرط غير صحيح في «{rname}»", f"A wrong condition in {rname}"))
            if f["type"] == "number":
                try:
                    float(c.get("value"))
                except (TypeError, ValueError) as exc:
                    raise ApiException(
                        code=ErrorCode.VALIDATION,
                        message=(f"اكتب رقماً في شرط «{f['ar']}»", f"Write a number in {f['en']}"),
                    ) from exc
            elif not str(c.get("value") if c.get("value") is not None else "").strip():
                raise ApiException(
                    code=ErrorCode.VALIDATION,
                    message=(f"اكتب قيمة شرط «{f['ar']}»", f"Write the value of {f['en']}"),
                )
        notify_roles = r.get("notify") or []
        if not notify_roles and not r.get("approval"):
            raise ApiException(
                code=ErrorCode.VALIDATION,
                message=(f"«{rname}» لا تفعل شيئاً: اختر من يُبلَّغ أو اطلب الموافقة", f"{rname} does nothing yet"),
            )

    field_ids: set[str] = set()
    seen_labels: set[tuple[str, str]] = set()
    for f in next_site["fields"]:
        if not isinstance(f, dict):
            raise ApiException(code=ErrorCode.VALIDATION, message=("بيانات الحقل غير صحيحة", "Invalid field entry"))
        fid = str(f.get("id") or "").strip()
        if not fid or fid in field_ids:
            raise ApiException(code=ErrorCode.VALIDATION, message=("حقل مكرر", "Duplicate field"))
        field_ids.add(fid)
        label = str(f.get("label") or "").strip()
        if not label:
            raise ApiException(code=ErrorCode.VALIDATION, message=("اسم الحقل مطلوب", "The field needs a name"))
        obj_type = f.get("object")
        if obj_type not in _VALID_CUSTOM_OBJECTS:
            raise ApiException(code=ErrorCode.VALIDATION, message=("اختر مكان الحقل", "Choose where the field goes"))
        if f.get("type") not in _VALID_CUSTOM_TYPES:
            raise ApiException(code=ErrorCode.VALIDATION, message=("نوع الحقل غير صحيح", "Wrong field type"))
        if f.get("type") == "list":
            opts = [str(o).strip() for o in (f.get("options") or []) if str(o).strip()]
            if not opts:
                raise ApiException(
                    code=ErrorCode.VALIDATION,
                    message=(f"أضف خيارات «{label}»", f"Add the choices of {label}"),
                )
        key = (str(obj_type), label)
        if key in seen_labels:
            raise ApiException(
                code=ErrorCode.VALIDATION,
                message=(f"يوجد حقل باسم «{label}»", "A field with this name exists"),
            )
        seen_labels.add(key)


@transaction.atomic
def save_site_config(*, actor: User, data: dict[str, Any], request: Any = None) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ApiException(code=ErrorCode.VALIDATION, message=("بيانات غير صالحة", "Invalid payload"))

    cfg = SiteConfig.objects.select_for_update().filter(pk=1).first() or SiteConfig.get_solo()
    old_site = serialize_site(cfg, request=request)

    raw_brand = data.get("brand") if isinstance(data.get("brand"), dict) else None
    merged_brand = {**copy.deepcopy(DEFAULT_SITE["brand"]), **copy.deepcopy(old_site["brand"])}
    if raw_brand is not None:
        merged_brand.update(raw_brand)
    if not str(merged_brand.get("name") or "").strip() or not str(merged_brand.get("company") or "").strip():
        msg = ("اسم الشركة مطلوب", "The company name is required")
        raise ApiException(code=ErrorCode.VALIDATION, message=msg, errors={"name": msg})

    new_logo_path = cfg.brand_logo_path
    if raw_brand is not None and "logo" in raw_brand:
        logo_val = raw_brand.get("logo")
        if logo_val is None or logo_val == "":
            if cfg.brand_logo_path:
                get_storage().delete("product-images", cfg.brand_logo_path)
            new_logo_path = ""
        elif isinstance(logo_val, str) and logo_val.startswith("data:image/"):
            if len(logo_val) > 400_000:
                raise ApiException(code=ErrorCode.VALIDATION, message=("الصورة كبيرة جداً", "The picture is too big"))
            raw_bytes, ext, mime_type = decode_image_data_url(logo_val, max_bytes=300 * 1024, max_dim=800)
            new_logo_path = get_storage().put_bytes("product-images", f"brand/logo.{ext}", raw_bytes, mime_type)

    if "look" in data:
        look = dict(data["look"]) if isinstance(data.get("look"), dict) else {}
        action_color = look.get("action")
        if action_color and not _HEX_COLOR_RE.match(str(action_color)):
            raise ApiException(code=ErrorCode.VALIDATION, message=("اللون غير صحيح", "The color isn't valid"))
    else:
        look = copy.deepcopy(old_site["look"])

    next_site = {
        "brand": merged_brand,
        "nav": (
            {**copy.deepcopy(DEFAULT_SITE["nav"]), **(data.get("nav") if isinstance(data.get("nav"), dict) else {})}
            if "nav" in data
            else copy.deepcopy(old_site["nav"])
        ),
        "dashboard": (
            {
                **copy.deepcopy(DEFAULT_SITE["dashboard"]),
                **(data.get("dashboard") if isinstance(data.get("dashboard"), dict) else {}),
            }
            if "dashboard" in data
            else copy.deepcopy(old_site["dashboard"])
        ),
        "categories": list(data.get("categories") or []) if "categories" in data else copy.deepcopy(old_site["categories"]),
        "texts": dict(data.get("texts") or {}) if "texts" in data else copy.deepcopy(old_site["texts"]),
        "layouts": dict(data.get("layouts") or {}) if "layouts" in data else copy.deepcopy(old_site["layouts"]),
        "look": look,
        "profiles": list(data.get("profiles") or []) if "profiles" in data else copy.deepcopy(old_site["profiles"]),
        "apps": list(data.get("apps") or []) if "apps" in data else copy.deepcopy(old_site["apps"]),
        "fields": list(data.get("fields") or []) if "fields" in data else copy.deepcopy(old_site["fields"]),
        "rules": list(data.get("rules") or []) if "rules" in data else copy.deepcopy(old_site["rules"]),
    }
    _validate_advanced_site(next_site)
    next_site["profiles"] = [p for raw in next_site["profiles"] if (p := _normalize_profile(raw)) is not None]
    next_site["apps"] = [a for raw in next_site["apps"] if (a := _normalize_app(raw)) is not None]
    next_site["fields"] = [f for raw in next_site["fields"] if (f := _normalize_field(raw)) is not None]
    next_site["rules"] = [r for raw in next_site["rules"] if (r := _normalize_rule(raw)) is not None]

    # Clear assigned profile_id on users if the profile was removed or its role changed.
    valid_profiles = {p["id"]: p for p in next_site["profiles"]}
    for u in User.objects.exclude(profile_id__isnull=True).exclude(profile_id=""):
        matched = valid_profiles.get(u.profile_id or "")
        if not matched or matched.get("role") != u.role:
            User.objects.filter(id=u.id).update(profile_id=None)

    cfg.brand_name = str(merged_brand.get("name") or "")[:150]
    cfg.brand_name_en = str(merged_brand.get("name_en") or "")[:150]
    cfg.brand_tagline = str(merged_brand.get("tagline") or "")[:255]
    cfg.brand_tagline_en = str(merged_brand.get("tagline_en") or "")[:255]
    cfg.brand_logo_path = new_logo_path
    cfg.brand_color = str(merged_brand.get("color") or "")[:32]
    extra_brand = {
        k: v
        for k, v in merged_brand.items()
        if k not in ("name", "name_en", "tagline", "tagline_en", "logo", "color")
    }
    cfg.brand_extra = extra_brand
    cfg.nav = next_site["nav"]
    cfg.dashboard = next_site["dashboard"]
    cfg.categories = next_site["categories"]
    cfg.texts = next_site["texts"]
    cfg.layouts = next_site["layouts"]
    cfg.look = next_site["look"]
    cfg.profiles = next_site["profiles"]
    cfg.apps = next_site["apps"]
    cfg.fields = next_site["fields"]
    cfg.rules = next_site["rules"]
    if "banners" in data and isinstance(data["banners"], list):
        cfg.banners = data["banners"]
    cfg.save()

    result = serialize_site(cfg, request=request)

    if old_site["brand"] != result["brand"]:
        record_audit(
            actor=actor,
            action="update",
            entity_type="settings",
            entity_id=0,
            summary=f"Site identity changed: {result['brand'].get('name_en')} ({result['brand'].get('company_en')})",
            before=old_site["brand"],
            after=result["brand"],
        )

    def _audit_diff(was: list[dict[str, Any]], now_list: list[dict[str, Any]], kind_en: str) -> None:
        was_by_id = {x.get("id"): x for x in was}
        now_by_id = {x.get("id"): x for x in now_list}
        for item in now_list:
            iid = item.get("id")
            name = item.get("name") or item.get("label") or iid
            prev = was_by_id.get(iid)
            if prev is None:
                record_audit(actor=actor, action="create", entity_type="settings", entity_id=0, summary=f"New {kind_en}: {name}")
            elif prev != item:
                record_audit(actor=actor, action="update", entity_type="settings", entity_id=0, summary=f"{kind_en} changed: {name}")
        for item in was:
            if item.get("id") not in now_by_id:
                name = item.get("name") or item.get("label") or item.get("id")
                record_audit(actor=actor, action="cancel", entity_type="settings", entity_id=0, summary=f"{kind_en} removed: {name}")

    _audit_diff(old_site["profiles"], result["profiles"], "profile")
    _audit_diff(old_site["apps"], result["apps"], "app")
    _audit_diff(old_site["fields"], result["fields"], "field")
    _audit_diff(old_site["rules"], result["rules"], "rule")
    from apps.accounts.business_rules import public_rules

    result["live"] = public_rules()
    return result


def profile_of(user: Any) -> dict[str, Any] | None:
    if not user or getattr(user, "role", None) == Role.MANAGER:
        return None
    pid = getattr(user, "profile_id", None)
    if not pid:
        return None
    cached = getattr(user, "_cached_profile", ...)
    if cached is not ...:
        return cached
    cfg = SiteConfig.objects.only("profiles").filter(pk=1).first()
    profiles = cfg.profiles if cfg and isinstance(cfg.profiles, list) else []
    found = next((p for p in profiles if isinstance(p, dict) and p.get("id") == pid and p.get("role") == user.role), None)
    user._cached_profile = found
    return found


def limits_of(user: Any) -> dict[str, Any]:
    prof = profile_of(user)
    if not prof or not isinstance(prof.get("limits"), dict):
        return {}
    return prof["limits"]


def in_hours(hours: dict[str, Any] | None, now: datetime | None = None) -> bool:
    if not hours:
        return True
    cur = now or baghdad_now()
    days = hours.get("days") or []
    # JS Date.getDay(): 0=Sunday .. 6=Saturday; Python weekday(): 0=Monday .. 6=Sunday
    js_day = (cur.weekday() + 1) % 7
    if js_day not in days:
        return False
    hm = cur.strftime("%H:%M")
    frm = str(hours.get("from") or "00:00")
    to = str(hours.get("to") or "23:59")
    if frm <= to:
        return frm <= hm < to
    return hm >= frm or hm < to


def may_profile_action(user: Any, action: str) -> None:
    prof = profile_of(user)
    if not prof:
        return
    denied_actions = prof.get("deny_actions") or limits_of(user).get("deny_actions") or []
    if action in denied_actions:
        raise ApiException(
            code=ErrorCode.FORBIDDEN,
            message=("ليس من صلاحيات ملفك تنفيذ هذا الإجراء", "Your profile doesn't allow this action"),
            status_code=http_status.HTTP_403_FORBIDDEN,
        )


def enforce_request_profile_access(user: Any, request: Any) -> None:
    """Enforce profile hours (401), read_only (403), and denied page routes (403)."""
    if not user or not getattr(user, "is_authenticated", False) or getattr(user, "role", None) == Role.MANAGER:
        return
    prof = profile_of(user)
    if not prof:
        return

    path = getattr(request, "path", "") or ""
    method = (getattr(request, "method", "GET") or "GET").upper()
    limits = limits_of(user)

    if (prof.get("read_only") or limits.get("read_only")) and method not in ("GET", "HEAD", "OPTIONS"):
        if not any(path.startswith(prefix) for prefix in _SELF_WRITE_PREFIXES):
            raise ApiException(
                code=ErrorCode.FORBIDDEN,
                message=("ملفك للعرض فقط: لا يمكنك تغيير شيء", "Your profile is see-only"),
                status_code=http_status.HTTP_403_FORBIDDEN,
            )

    denied_pages = prof.get("denied") or []
    # Reference endpoints required by AppShell on every page load must remain readable on GET.
    _APP_SHELL_READ_PATHS = ("/api/v1/items", "/api/v1/categories", "/api/v1/price-tiers")
    if denied_pages:
        for page_prefix, api_prefixes in _DENIED_PAGE_API_MAP:
            if any(p == page_prefix or p.startswith(page_prefix + "/") for p in denied_pages):
                if any(path == ap or path.startswith(ap + "/") for ap in api_prefixes):
                    if method in ("GET", "HEAD", "OPTIONS") and any(
                        path == rp or path.startswith(rp + "/") for rp in _APP_SHELL_READ_PATHS
                    ):
                        continue
                    raise ApiException(
                        code=ErrorCode.FORBIDDEN,
                        message=("هذه الصفحة محجوبة عن ملف صلاحياتك", "Your profile does not allow access to this section"),
                        status_code=http_status.HTTP_403_FORBIDDEN,
                    )


# ---------------- Custom Fields ----------------


def custom_fields_for(object_type: str) -> list[dict[str, Any]]:
    cfg = SiteConfig.objects.only("fields").filter(pk=1).first()
    fields = cfg.fields if cfg and isinstance(cfg.fields, list) else []
    return [
        f
        for f in fields
        if isinstance(f, dict) and f.get("object") == object_type and f.get("active", True) is not False
    ]


def may_view_custom_field(field_def: dict[str, Any], actor: Any) -> bool:
    if not actor or getattr(actor, "role", None) == Role.MANAGER:
        return True
    view_roles = field_def.get("view_roles") or []
    return not view_roles or getattr(actor, "role", None) in view_roles


def may_edit_custom_field(field_def: dict[str, Any], actor: Any) -> bool:
    if not may_view_custom_field(field_def, actor):
        return False
    if getattr(actor, "role", None) == Role.MANAGER:
        return True
    edit_roles = field_def.get("edit_roles") or []
    return not edit_roles or getattr(actor, "role", None) in edit_roles


def _empty_custom_value(val: Any) -> bool:
    return val is None or val == "" or (isinstance(val, list) and len(val) == 0)


def clean_custom_value(field_def: dict[str, Any], raw: Any) -> Any:
    if raw is None or raw == "":
        return None
    ftype = field_def.get("type")
    label = str(field_def.get("label") or field_def.get("id"))
    label_en = str(field_def.get("label_en") or label)
    if ftype == "text":
        val = str(raw).strip()
        if not val:
            return None
        if len(val) > 500:
            raise ApiException(code=ErrorCode.VALIDATION, message=(f"«{label}» طويل جداً", f"{label_en} is too long"))
        return val
    if ftype == "number":
        try:
            num = float(raw)
        except (TypeError, ValueError) as exc:
            raise ApiException(code=ErrorCode.VALIDATION, message=(f"«{label}» يجب أن يكون رقماً", f"{label_en} must be a number")) from exc
        import math

        if not math.isfinite(num):
            raise ApiException(code=ErrorCode.VALIDATION, message=(f"«{label}» غير صالح", f"{label_en} is invalid"))
        return int(num) if num.is_integer() else num
    if ftype == "date":
        val = str(raw).strip()
        if not _DATE_RE.match(val):
            raise ApiException(code=ErrorCode.VALIDATION, message=(f"«{label}» تاريخ غير صحيح", f"{label_en} must be YYYY-MM-DD"))
        return val
    if ftype == "list":
        val = str(raw).strip()
        opts = [str(o) for o in (field_def.get("options") or [])]
        if val not in opts:
            raise ApiException(code=ErrorCode.VALIDATION, message=(f"«{label}»: اختر من القائمة", f"{label_en}: choose from list"))
        return val
    if ftype == "yesno":
        if isinstance(raw, bool):
            return raw
        if str(raw).lower() in ("true", "1", "yes"):
            return True
        if str(raw).lower() in ("false", "0", "no"):
            return False
        raise ApiException(code=ErrorCode.VALIDATION, message=(f"«{label}»: نعم أو لا", f"{label_en}: yes or no"))
    return None


def custom_values_for_object(
    object_type: str,
    object_id: int,
    actor: Any = None,
    *,
    raw_values: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    if raw_values is None:
        row = CustomFieldValue.objects.filter(object_type=object_type, object_id=object_id).first()
        raw_values = dict(row.values) if row and isinstance(row.values, dict) else {}
    if not raw_values:
        return None
    fields = {f["id"]: f for f in custom_fields_for(object_type)}
    out: dict[str, Any] = {}
    for fid, val in raw_values.items():
        fdef = fields.get(fid)
        if not fdef or val is None:
            continue
        if actor is not None and not may_view_custom_field(fdef, actor):
            continue
        out[fid] = val
    return out or None


def batch_custom_values(object_type: str, object_ids: list[int], actor: Any = None) -> dict[int, dict[str, Any]]:
    if not object_ids:
        return {}
    fields = {f["id"]: f for f in custom_fields_for(object_type)}
    if not fields:
        return {}
    rows = CustomFieldValue.objects.filter(object_type=object_type, object_id__in=object_ids)
    result: dict[int, dict[str, Any]] = {}
    for row in rows:
        raw = row.values if isinstance(row.values, dict) else {}
        filtered: dict[str, Any] = {}
        for fid, val in raw.items():
            fdef = fields.get(fid)
            if fdef and val is not None and (actor is None or may_view_custom_field(fdef, actor)):
                filtered[fid] = val
        if filtered:
            result[row.object_id] = filtered
    return result


def validate_and_apply_custom_on_create(
    object_type: str, object_id: int, actor: Any, raw_custom: dict[str, Any] | None
) -> dict[str, Any] | None:
    """Apply and validate custom field values provided during object creation (customer/item/order)."""
    fields = custom_fields_for(object_type)
    if not fields:
        return None
    incoming = raw_custom if isinstance(raw_custom, dict) else {}
    next_vals: dict[str, Any] = {}
    errors: dict[str, tuple[str, str]] = {}
    for fdef in fields:
        fid = fdef["id"]
        if fid in incoming:
            if not may_edit_custom_field(fdef, actor):
                raise ApiException(
                    code=ErrorCode.FORBIDDEN,
                    message=(f"لا تملك تعديل «{fdef['label']}»", f"You can't change {fdef.get('label_en') or fdef['label']}"),
                    status_code=http_status.HTTP_403_FORBIDDEN,
                )
            cleaned = clean_custom_value(fdef, incoming[fid])
            if cleaned is not None:
                next_vals[fid] = cleaned
        if fdef.get("required") and may_edit_custom_field(fdef, actor) and _empty_custom_value(next_vals.get(fid)):
            label = str(fdef["label"])
            label_en = str(fdef.get("label_en") or label)
            errors[f"custom.{fid}"] = (f"«{label}» مطلوب", f"{label_en} is required")
    if errors:
        raise ApiException(
            code=ErrorCode.VALIDATION,
            message=("تحقق من الحقول", "Check the fields"),
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            errors=errors,
        )
    if next_vals:
        CustomFieldValue.objects.update_or_create(
            object_type=object_type,
            object_id=object_id,
            defaults={"values": next_vals},
        )
    return custom_values_for_object(object_type, object_id, actor, raw_values=next_vals)


@transaction.atomic
def save_custom_values(
    *, actor: User, object_type: str, object_id: int, values: dict[str, Any]
) -> dict[str, Any]:
    if object_type not in _VALID_CUSTOM_OBJECTS:
        raise ApiException(code=ErrorCode.NOT_FOUND, status_code=http_status.HTTP_404_NOT_FOUND)
    if not isinstance(values, dict):
        raise ApiException(code=ErrorCode.VALIDATION, message=("البيانات غير صالحة", "Invalid custom field values"))

    record_name = ""
    if object_type == "customer":
        from apps.customers.models import Customer
        from apps.customers.services import can_see_customer
        from apps.deliveries.models import Delivery

        cust = Customer.objects.filter(id=object_id).first()
        delivers_to = Delivery.objects.filter(order__customer_id=object_id, rep=actor).exists()
        if not cust or not (can_see_customer(actor, cust) or delivers_to):
            raise ApiException(code=ErrorCode.NOT_FOUND, status_code=http_status.HTTP_404_NOT_FOUND)
        record_name = cust.name
    elif object_type == "item":
        from apps.catalog.models import Item

        item = Item.objects.filter(id=object_id).first()
        if not item:
            raise ApiException(code=ErrorCode.NOT_FOUND, status_code=http_status.HTTP_404_NOT_FOUND)
        record_name = item.name
    else:
        from apps.orders.models import Order
        from apps.orders.services import can_see_order

        order = Order.objects.filter(id=object_id).first()
        if not order or not can_see_order(actor, order):
            raise ApiException(code=ErrorCode.NOT_FOUND, status_code=http_status.HTTP_404_NOT_FOUND)
        record_name = order.number

    row, _ = CustomFieldValue.objects.select_for_update().get_or_create(
        object_type=object_type, object_id=object_id, defaults={"values": {}}
    )
    old_vals = dict(row.values) if isinstance(row.values, dict) else {}
    next_vals = dict(old_vals)
    fields = custom_fields_for(object_type)
    changed_labels: list[str] = []

    for fdef in fields:
        fid = fdef["id"]
        if fid not in values:
            continue
        cleaned = clean_custom_value(fdef, values[fid])
        if cleaned == old_vals.get(fid):
            continue
        if not may_edit_custom_field(fdef, actor):
            raise ApiException(
                code=ErrorCode.FORBIDDEN,
                message=(f"لا تملك تعديل «{fdef['label']}»", f"You can't change {fdef.get('label_en') or fdef['label']}"),
                status_code=http_status.HTTP_403_FORBIDDEN,
            )
        if cleaned is None:
            next_vals.pop(fid, None)
        else:
            next_vals[fid] = cleaned
        changed_labels.append(str(fdef["label"]))

    errors: dict[str, tuple[str, str]] = {}
    for fdef in fields:
        fid = fdef["id"]
        if fdef.get("required") and may_edit_custom_field(fdef, actor) and _empty_custom_value(next_vals.get(fid)):
            label = str(fdef["label"])
            label_en = str(fdef.get("label_en") or label)
            errors[f"custom.{fid}"] = (f"«{label}» مطلوب", f"{label_en} is required")
    if errors:
        raise ApiException(
            code=ErrorCode.VALIDATION,
            message=("تحقق من الحقول", "Check the fields"),
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            errors=errors,
        )

    if next_vals:
        row.values = next_vals
        row.save(update_fields=["values", "updated_at"])
    else:
        row.delete()

    if changed_labels:
        record_audit(
            actor=actor,
            action="update",
            entity_type=object_type,
            entity_id=object_id,
            summary=f"{record_name}: updated custom fields ({', '.join(changed_labels)})",
            before=old_vals,
            after=next_vals,
        )
    return custom_values_for_object(object_type, object_id, actor, raw_values=next_vals) or {}
