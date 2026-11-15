"""Advanced business rules («قواعد العمل») defaults, validation, diffing, and helpers.

Mirrors `frontend/lib/api/advanced.ts`, `frontend/lib/api/permissions.ts`, and
`frontend/docs/backend.md` Sections 3.6 & 3.7.
"""
from __future__ import annotations

import copy
import re
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from apps.common.utils import baghdad_now, round_by

BAGHDAD_TZ = ZoneInfo("Asia/Baghdad")

ROLES_ALL: tuple[str, ...] = ("manager", "accountant", "warehouse", "rep", "staff", "shop")
STAFF_ROLES: tuple[str, ...] = ("manager", "accountant", "warehouse", "staff", "rep")
DOOR_METHODS_ALL: tuple[str, ...] = ("cash", "qi", "zaincash")
ROUND_TO_STEPS: tuple[int, ...] = (0, 50, 100, 250, 500, 1000)
ANNOUNCEMENT_COLORS: tuple[str, ...] = ("amber", "red", "green", "blue")
FEATURE_KEYS: tuple[str, ...] = (
    "online_pay",
    "self_signup",
    "ratings",
    "clearance",
    "reorder",
    "notify_me",
    "support_chat",
    "offline",
)

DEFAULT_PREFIXES: dict[str, str] = {
    "order": "ORD",
    "invoice": "INV",
    "payment": "PAY",
    "return": "RET",
    "receipt": "RCV",
    "transfer": "TRF",
    "supply": "SUP",
    "expense": "EXP",
    "credit_note": "CN",
    "refund": "RF",
    "stocktake": "CNT",
    "handover": "HND",
}

ACTIONS: dict[str, dict[str, Any]] = {
    "record_payment": {"group": "money", "ar": "تسجيل تسديد", "en": "Record a payment", "def": ["manager", "accountant", "rep"]},
    "reverse_payment": {"group": "money", "ar": "عكس تسديد", "en": "Reverse a payment", "def": ["manager", "accountant"]},
    "refund": {"group": "money", "ar": "ردّ مبلغ لمحل", "en": "Refund a shop", "def": ["manager", "accountant"]},
    "credit_note": {"group": "money", "ar": "إشعار دائن على فاتورة", "en": "Credit note", "def": ["manager", "accountant"]},
    "approve_expense": {"group": "money", "ar": "الموافقة على صرفية", "en": "Approve an expense", "def": ["manager"]},
    "set_usd_rate": {"group": "money", "ar": "تحديد سعر الدولار", "en": "Set the dollar rate", "def": ["manager", "accountant"]},
    "close_month": {"group": "money", "ar": "إغلاق شهر العمولات", "en": "Close a commission month", "def": ["manager"]},
    "approve_order": {"group": "orders", "ar": "اعتماد طلب", "en": "Approve an order", "def": ["manager", "accountant"]},
    "cancel_order": {"group": "orders", "ar": "إلغاء طلب", "en": "Cancel an order", "def": ["manager", "accountant"]},
    "give_discount": {"group": "orders", "ar": "إعطاء خصم", "en": "Give a discount", "def": ["manager", "accountant", "rep"]},
    "approve_over_limit": {"group": "orders", "ar": "الموافقة على تجاوز السقف والخصم الكبير", "en": "OK over the limit / a big discount", "def": ["manager"]},
    "unlock_delivery": {"group": "orders", "ar": "فتح شحنة مقفلة", "en": "Unlock a locked shipment", "def": ["manager"]},
    "take_back_shipment": {"group": "orders", "ar": "إرجاع شحنة إلى المخزن", "en": "Take a shipment back", "def": ["manager", "warehouse"]},
    "edit_items": {"group": "stock", "ar": "تعديل المواد والأسعار", "en": "Edit items and prices", "def": ["manager"]},
    "start_clearance": {"group": "stock", "ar": "بدء عرض تصفية", "en": "Start a clearance", "def": ["manager"]},
    "hold_batch": {"group": "stock", "ar": "إيقاف دفعة وإطلاقها", "en": "Hold / release a batch", "def": ["manager", "warehouse"]},
    "close_stocktake": {"group": "stock", "ar": "إغلاق الجرد واعتماد فروقه", "en": "Close a stock count", "def": ["manager"]},
    "review_transfer": {"group": "stock", "ar": "مراجعة تحويل وصل ناقصاً", "en": "Review a short transfer", "def": ["manager"]},
    "receive_goods": {"group": "stock", "ar": "استلام بضاعة", "en": "Receive goods", "def": ["manager", "warehouse"]},
    "approve_shop": {"group": "people", "ar": "الموافقة على محل جديد أو رفضه", "en": "Approve / refuse a new shop", "def": ["manager"]},
    "edit_customers": {"group": "people", "ar": "تعديل الزبائن وسقف الدين", "en": "Edit customers and debt limits", "def": ["manager"]},
    "manage_users": {"group": "people", "ar": "إضافة المستخدمين وإيقافهم", "en": "Add and stop users", "def": ["manager"]},
    "see_costs": {"group": "people", "ar": "رؤية الكلفة والربح", "en": "See costs and profit", "def": ["manager", "accountant"]},
    "see_debts": {"group": "people", "ar": "رؤية الديون", "en": "See debts", "def": ["manager", "accountant", "rep"]},
}

DEFAULT_NAMES: dict[str, Any] = {
    "prefixes": {},
    "currency_ar": "د.ع",
    "currency_en": "IQD",
    "invoice_footer": "",
}

DEFAULT_WORK: dict[str, Any] = {
    "on": False,
    "days": [6, 0, 1, 2, 3, 4],
    "from": "08:00",
    "to": "18:00",
    "holidays": [],
}

DEFAULT_DELIVERY_PLAN: dict[str, Any] = {
    "on": False,
    "cutoff": "14:00",
    "days_by_area": {},
}

DEFAULT_RULES: dict[str, Any] = {
    "staff_discount_max": 3,
    "discount_max": 30,
    "round_to": 250,
    "offline_max_days": 3,
    "over_limit": "approval",
    "min_shop_order": 0,
    "shop_edit_minutes": 0,
    "auto_confirm_rep_orders": False,
    "pin_tries": 5,
    "photo_required": True,
    "door_methods": ["cash", "qi", "zaincash"],
    "low_rating_stars": 2,
    "custody_cap": 0,
    "reverse_roles": ["manager", "accountant"],
    "reverse_days": 0,
    "signup_tier_id": None,
    "signup_debt_limit": 0,
    "signup_auto_approve": False,
    "max_shops_per_account": 10,
    "visit_near_m": 200,
    "far_visit_alert": False,
    "reorder_early_days": 1,
    "otp": {
        "code_minutes": 5,
        "resend_seconds": 60,
        "per_hour": 5,
        "tries": 5,
    },
    "login_lock_after": 0,
    "login_lock_minutes": 15,
    "password_min": 6,
    "permissions": {},
    "people": {},
    "discount_by_role": {},
    "expense_by_role": {},
    "work": copy.deepcopy(DEFAULT_WORK),
    "delivery_plan": copy.deepcopy(DEFAULT_DELIVERY_PLAN),
    "warehouses": {},
    "names": copy.deepcopy(DEFAULT_NAMES),
    "features": {k: True for k in FEATURE_KEYS},
    "maintenance": {
        "on": False,
        "message": "",
        "message_en": "",
    },
    "announcement": None,
    "notify_matrix": {},
}

# Mirrors `DEFAULT_NOTIFY` in `frontend/lib/api/advanced.ts`
DEFAULT_NOTIFY: dict[str, list[str]] = {
    "order_pending": ["warehouse", "manager"],
    "order_accepted": ["warehouse", "shop"],
    "order_confirmed": ["warehouse", "rep", "shop"],
    "needs_approval": ["manager", "accountant"],
    "payment": ["manager", "accountant", "shop"],
    "payment_reversed": ["manager", "accountant", "shop"],
    "refund": ["manager", "accountant", "shop"],
    "expense": ["manager"],
    "commission_earned": ["rep", "accountant"],
    "new_shop": ["manager", "accountant"],
    "batch_held": ["warehouse", "manager", "accountant"],
    "transfer_sent": ["warehouse"],
    "transfer_discrepancy": ["manager", "warehouse"],
    "custody": ["manager", "accountant", "rep"],
    "supply_order": ["warehouse", "manager", "accountant"],
    "usd_rate": ["manager", "accountant", "rep", "warehouse"],
    "stocktake_closed": ["manager", "accountant", "warehouse"],
    "delivery_problem": ["manager", "warehouse", "rep"],
    "delivery_locked": ["manager", "warehouse", "rep"],
    "back_in_stock": ["shop"],
    "price_change": ["shop", "rep"],
    "clearance": ["shop", "rep"],
    "rule": ["manager"],
}

NOTIFY_KIND_ALIASES: dict[str, str] = {
    "order_needs_approval": "needs_approval",
    "order_review": "order_pending",
    "order_waiting_stock": "order_pending",
    "order_partial_prep": "order_accepted",
    "delivery_ready": "order_accepted",
    "out_for_delivery": "order_confirmed",
    "delivered": "order_confirmed",
    "shop_registered": "new_shop",
    "branch_added": "new_shop",
    "shop_approved": "new_shop",
    "shop_rejected": "new_shop",
}

_DATE_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_PREFIX_RE = re.compile(r"^[A-Z]{1,6}$")


def canonical_notify_kind(kind: str) -> str:
    return NOTIFY_KIND_ALIASES.get(kind, kind)


def rules_of(setting_or_advanced: Any = None) -> dict[str, Any]:
    """Merge sparse stored `advanced` rules with `DEFAULT_RULES`, matching frontend `rulesOf()`."""
    base = copy.deepcopy(DEFAULT_RULES)
    if setting_or_advanced is None:
        from apps.accounts.models import SystemSetting

        setting_obj = SystemSetting.get_solo()
        raw_adv = getattr(setting_obj, "advanced", None) or {}
        auto_confirm = bool(setting_obj.auto_confirm_rep_orders)
    elif isinstance(setting_or_advanced, dict):
        raw_adv = setting_or_advanced
        auto_confirm = bool(raw_adv.get("auto_confirm_rep_orders", False))
    else:
        raw_adv = getattr(setting_or_advanced, "advanced", None) or {}
        auto_confirm = bool(getattr(setting_or_advanced, "auto_confirm_rep_orders", False))

    if not isinstance(raw_adv, dict):
        raw_adv = {}

    for key, val in raw_adv.items():
        if key not in DEFAULT_RULES:
            continue
        if key == "otp" and isinstance(val, dict):
            base["otp"] = {**base["otp"], **{k: v for k, v in val.items() if k in base["otp"]}}
        elif key == "features" and isinstance(val, dict):
            base["features"] = {**base["features"], **{k: bool(v) for k, v in val.items() if k in FEATURE_KEYS}}
        elif key == "maintenance" and isinstance(val, dict):
            base["maintenance"] = {
                "on": bool(val.get("on", False)),
                "message": str(val.get("message") or ""),
                "message_en": str(val.get("message_en") or ""),
            }
        elif key == "names" and isinstance(val, dict):
            raw_pre = val.get("prefixes") if isinstance(val.get("prefixes"), dict) else {}
            base["names"] = {
                "prefixes": {str(k): str(v) for k, v in raw_pre.items() if k in DEFAULT_PREFIXES and isinstance(v, str) and v},
                "currency_ar": str(val.get("currency_ar") if val.get("currency_ar") is not None else DEFAULT_NAMES["currency_ar"]),
                "currency_en": str(val.get("currency_en") if val.get("currency_en") is not None else DEFAULT_NAMES["currency_en"]),
                "invoice_footer": str(val.get("invoice_footer") or ""),
            }
        elif key == "work" and isinstance(val, dict):
            base["work"] = {
                "on": bool(val.get("on", False)),
                "days": list(val.get("days")) if isinstance(val.get("days"), list) else list(DEFAULT_WORK["days"]),
                "from": str(val.get("from") if val.get("from") is not None else DEFAULT_WORK["from"]),
                "to": str(val.get("to") if val.get("to") is not None else DEFAULT_WORK["to"]),
                "holidays": copy.deepcopy(val.get("holidays")) if isinstance(val.get("holidays"), list) else [],
            }
        elif key == "delivery_plan" and isinstance(val, dict):
            base["delivery_plan"] = {
                "on": bool(val.get("on", False)),
                "cutoff": str(val.get("cutoff") if val.get("cutoff") is not None else DEFAULT_DELIVERY_PLAN["cutoff"]),
                "days_by_area": copy.deepcopy(val.get("days_by_area")) if isinstance(val.get("days_by_area"), dict) else {},
            }
        elif key == "permissions" and isinstance(val, dict):
            base["permissions"] = {
                str(k): [str(r) for r in roles if isinstance(r, str)]
                for k, roles in val.items()
                if isinstance(roles, list)
            }
        elif key == "people" and isinstance(val, dict):
            base["people"] = copy.deepcopy(val)
        elif key in ("discount_by_role", "expense_by_role", "warehouses") and isinstance(val, dict):
            base[key] = copy.deepcopy(val)
        elif key == "announcement":
            base["announcement"] = copy.deepcopy(val) if isinstance(val, dict) else None
        elif key == "notify_matrix" and isinstance(val, dict):
            base["notify_matrix"] = {
                str(k): [str(r) for r in roles if isinstance(r, str)]
                for k, roles in val.items()
                if isinstance(roles, list)
            }
        elif key in ("door_methods", "reverse_roles") and isinstance(val, list):
            base[key] = [str(v) for v in val if isinstance(v, str)]
        else:
            base[key] = copy.deepcopy(val)

    # Backward compatibility: `reverse_roles` maps into `permissions.reverse_payment` if not set
    if "reverse_roles" in raw_adv and "reverse_payment" not in base["permissions"]:
        if isinstance(raw_adv.get("reverse_roles"), list):
            base["permissions"]["reverse_payment"] = [str(r) for r in raw_adv["reverse_roles"] if isinstance(r, str)]

    if isinstance(setting_or_advanced, dict):
        if "auto_confirm_rep_orders" in raw_adv:
            base["auto_confirm_rep_orders"] = bool(raw_adv["auto_confirm_rep_orders"])
    else:
        base["auto_confirm_rep_orders"] = auto_confirm
    return base


def roles_for(perms: dict[str, Any] | None, action_key: str) -> list[str]:
    """Return allowed roles for `action_key` (always includes `'manager'`)."""
    raw = (perms or {}).get(action_key)
    roles = list(raw) if isinstance(raw, list) else list(ACTIONS[action_key]["def"])
    if "manager" not in roles:
        roles = ["manager", *roles]
    return roles


def user_may_in_rules(rules: dict[str, Any], user: Any, action_key: str) -> bool:
    """Evaluate `userMay()` ordering: manager -> people[id].deny -> people[id].allow -> permissions[action]."""
    if not user or not getattr(user, "is_authenticated", True):
        return False
    role = getattr(user, "role", None)
    if role == "manager":
        return True
    if role == "shop":
        return False
    people = rules.get("people") if isinstance(rules.get("people"), dict) else {}
    p = people.get(str(getattr(user, "id", "")))
    if isinstance(p, dict):
        deny = p.get("deny")
        if isinstance(deny, list) and action_key in deny:
            return False
        allow = p.get("allow")
        if isinstance(allow, list) and action_key in allow:
            return True
    perms = rules.get("permissions") if isinstance(rules.get("permissions"), dict) else {}
    return role in roles_for(perms, action_key)


def prefix_from_rules(rules: dict[str, Any], kind: str) -> str:
    names = rules.get("names") if isinstance(rules.get("names"), dict) else {}
    prefixes = names.get("prefixes") if isinstance(names.get("prefixes"), dict) else {}
    custom = prefixes.get(kind)
    if isinstance(custom, str) and custom.strip():
        return custom.strip()
    return DEFAULT_PREFIXES[kind]


def prefix_of(kind: str, setting: Any = None) -> str:
    return prefix_from_rules(rules_of(setting), kind)


def discount_alone(rules: dict[str, Any], user: Any) -> Decimal:
    """Discount % actor can grant without manager approval: person -> role -> staff_discount_max."""
    people = rules.get("people") if isinstance(rules.get("people"), dict) else {}
    p = people.get(str(getattr(user, "id", "")))
    if isinstance(p, dict) and p.get("discount_max") is not None:
        return Decimal(str(p["discount_max"]))
    by_role = rules.get("discount_by_role") if isinstance(rules.get("discount_by_role"), dict) else {}
    role = getattr(user, "role", None)
    if role in by_role and by_role[role] is not None:
        return Decimal(str(by_role[role]))
    return Decimal(str(rules.get("staff_discount_max", 3)))


def custody_cap_of(rules: dict[str, Any], rep_id: int) -> int:
    """Cash custody alert limit for `rep_id`: person -> global custody_cap."""
    people = rules.get("people") if isinstance(rules.get("people"), dict) else {}
    p = people.get(str(rep_id))
    if isinstance(p, dict) and p.get("custody_cap") is not None:
        return int(p["custody_cap"])
    return int(rules.get("custody_cap") or 0)


def expense_limit_of(rules: dict[str, Any], user: Any, base_limit: int) -> int:
    """Expense approval threshold for `user`: person -> role -> base setting limit."""
    people = rules.get("people") if isinstance(rules.get("people"), dict) else {}
    p = people.get(str(getattr(user, "id", "")))
    if isinstance(p, dict) and p.get("expense_limit") is not None:
        return int(p["expense_limit"])
    by_role = rules.get("expense_by_role") if isinstance(rules.get("expense_by_role"), dict) else {}
    role = getattr(user, "role", None)
    if role in by_role and by_role[role] is not None:
        return int(by_role[role])
    return int(base_limit)


def warehouse_rule(rules: dict[str, Any], warehouse_id: int | None) -> dict[str, Any]:
    if warehouse_id is None:
        return {}
    whs = rules.get("warehouses") if isinstance(rules.get("warehouses"), dict) else {}
    entry = whs.get(str(warehouse_id))
    return entry if isinstance(entry, dict) else {}


def warehouse_reservation_hours(warehouse_id: int | None, setting: Any = None) -> int:
    from apps.accounts.models import SystemSetting

    setting_obj = setting if setting is not None else SystemSetting.get_solo()
    rules = rules_of(setting_obj)
    w_rule = warehouse_rule(rules, warehouse_id)
    if w_rule.get("reservation_hours") is not None:
        return int(w_rule["reservation_hours"])
    return int(getattr(setting_obj, "reservation_hours", 24) or 24)


def _to_baghdad_dt(dt: datetime | None) -> datetime:
    cur = dt or baghdad_now()
    if cur.tzinfo is None:
        return cur.replace(tzinfo=BAGHDAD_TZ)
    return cur.astimezone(BAGHDAD_TZ)


def _js_weekday(d: date | datetime) -> int:
    """Return JS `Date.getDay()` numbering: 0=Sunday .. 6=Saturday."""
    return (d.weekday() + 1) % 7


def _hhmm_to_minutes(hhmm: str) -> int:
    h_str, m_str = hhmm.split(":")
    return int(h_str) * 60 + int(m_str)


def is_work_day(rules: dict[str, Any], dt_or_date: date | datetime | None = None) -> bool:
    """Return True if `dt_or_date` (Baghdad local) is an open working day."""
    work = rules.get("work") if isinstance(rules.get("work"), dict) else {}
    if not work.get("on"):
        return True
    d = _to_baghdad_dt(dt_or_date).date() if isinstance(dt_or_date, datetime) or dt_or_date is None else dt_or_date
    days = work.get("days") if isinstance(work.get("days"), list) else DEFAULT_WORK["days"]
    if _js_weekday(d) not in days:
        return False
    ymd = d.isoformat()
    holidays = work.get("holidays") if isinstance(work.get("holidays"), list) else []
    return not any(isinstance(h, dict) and str(h.get("date") or "") == ymd for h in holidays)


def in_work_time(rules: dict[str, Any], dt: datetime | None = None) -> bool:
    work = rules.get("work") if isinstance(rules.get("work"), dict) else {}
    if not work.get("on"):
        return True
    local = _to_baghdad_dt(dt)
    if not is_work_day(rules, local.date()):
        return False
    mins = local.hour * 60 + local.minute
    start_m = _hhmm_to_minutes(str(work.get("from") or DEFAULT_WORK["from"]))
    end_m = _hhmm_to_minutes(str(work.get("to") or DEFAULT_WORK["to"]))
    return start_m <= mins < end_m


def next_work_start(rules: dict[str, Any], dt: datetime | None = None) -> datetime:
    """Return `dt` (in Baghdad local) if inside working hours, else next opening timestamp."""
    local = _to_baghdad_dt(dt)
    if in_work_time(rules, local):
        return local
    work = rules.get("work") if isinstance(rules.get("work"), dict) else {}
    from_str = str(work.get("from") or DEFAULT_WORK["from"])
    fh, fm = (int(x) for x in from_str.split(":"))
    start_mins = fh * 60 + fm
    if is_work_day(rules, local.date()) and (local.hour * 60 + local.minute) < start_mins:
        return datetime.combine(local.date(), time(fh, fm), tzinfo=BAGHDAD_TZ)
    for offset in range(1, 61):
        cand_date = local.date() + timedelta(days=offset)
        if is_work_day(rules, cand_date):
            return datetime.combine(cand_date, time(fh, fm), tzinfo=BAGHDAD_TZ)
    return local


def hold_from(
    warehouse_id: int | None,
    *,
    now: datetime | None = None,
    setting: Any = None,
) -> tuple[datetime, datetime | None]:
    """Return `(reserved_until_utc, after_hours_utc_or_none)` honoring `work` and per-warehouse hours."""
    from apps.accounts.models import SystemSetting

    setting_obj = setting if setting is not None else SystemSetting.get_solo()
    rules = rules_of(setting_obj)
    local_now = _to_baghdad_dt(now)
    hours = warehouse_reservation_hours(warehouse_id, setting_obj)
    if rules.get("work", {}).get("on"):
        start = next_work_start(rules, local_now)
        until_dt = (start + timedelta(hours=hours)).astimezone(UTC)
        after_dt = start.astimezone(UTC) if start > local_now else None
        return until_dt, after_dt
    return (local_now + timedelta(hours=hours)).astimezone(UTC), None


def expected_delivery(rules: dict[str, Any], area: str, dt: datetime | None = None) -> date | None:
    """Return expected delivery date for `area` under `delivery_plan`, or `None` when disabled."""
    dp = rules.get("delivery_plan") if isinstance(rules.get("delivery_plan"), dict) else {}
    if not dp.get("on"):
        return None
    local = _to_baghdad_dt(dt)
    days_by_area = dp.get("days_by_area") if isinstance(dp.get("days_by_area"), dict) else {}
    area_days = days_by_area.get(area) if area else None
    cutoff = str(dp.get("cutoff") or "").strip()
    before_cut = (not cutoff) or ((local.hour * 60 + local.minute) < _hhmm_to_minutes(cutoff))
    start_offset = 0 if before_cut else 1
    for offset in range(start_offset, 31):
        cand = local.date() + timedelta(days=offset)
        if not is_work_day(rules, cand):
            continue
        if isinstance(area_days, list) and len(area_days) > 0 and _js_weekday(cand) not in area_days:
            continue
        return cand
    return None


def public_rules(rules: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return unauthenticated `site().live` rules slice matching `publicRules()`."""
    r = rules if rules is not None else rules_of()
    names = r.get("names") if isinstance(r.get("names"), dict) else {}
    perms = r.get("permissions") if isinstance(r.get("permissions"), dict) else {}
    manage_roles = roles_for(perms, "manage_users")
    people = r.get("people") if isinstance(r.get("people"), dict) else {}
    allow_users: list[int] = []
    deny_users: list[int] = []
    for uid_str, exc in people.items():
        if not isinstance(exc, dict):
            continue
        try:
            uid = int(uid_str)
        except (TypeError, ValueError):
            continue
        if isinstance(exc.get("allow"), list) and "manage_users" in exc["allow"]:
            allow_users.append(uid)
        if isinstance(exc.get("deny"), list) and "manage_users" in exc["deny"]:
            deny_users.append(uid)
    return {
        "self_signup": bool(r["features"]["self_signup"]),
        "features": copy.deepcopy(r["features"]),
        "maintenance": copy.deepcopy(r["maintenance"]),
        "announcement": copy.deepcopy(r["announcement"]),
        "password_min": int(r["password_min"]),
        "names": {
            "currency_ar": str(names.get("currency_ar") or DEFAULT_NAMES["currency_ar"]),
            "currency_en": str(names.get("currency_en") or DEFAULT_NAMES["currency_en"]),
            "invoice_footer": str(names.get("invoice_footer") or ""),
        },
        "users_page": {
            "roles": manage_roles,
            "allow": allow_users,
            "deny": deny_users,
        },
    }


def _clean_announcement(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    text = str(raw.get("text") or "").strip()
    text_en = str(raw.get("text_en") or "").strip()
    color = raw.get("color") if raw.get("color") in ANNOUNCEMENT_COLORS else "amber"
    until = str(raw.get("until") or "").strip()
    if not text and not text_en and not until:
        return None
    out: dict[str, Any] = {
        "text": text,
        "text_en": text_en,
        "color": color,
    }
    if isinstance(raw.get("roles"), list):
        out["roles"] = [str(r) for r in raw["roles"] if isinstance(r, str)]
    if raw.get("tone") in ("info", "warning"):
        out["tone"] = raw["tone"]
    if until:
        out["until"] = until
    return out


def changed_rules(rules: dict[str, Any]) -> dict[str, Any]:
    """Strip keys matching `DEFAULT_RULES`, mirroring `changedRules()` in `advanced.ts`."""
    out: dict[str, Any] = {}
    for key, default_val in DEFAULT_RULES.items():
        if key not in rules:
            continue
        val = rules[key]
        if key == "announcement":
            cleaned = _clean_announcement(val)
            if cleaned is not None:
                out["announcement"] = cleaned
            continue
        if key == "notify_matrix":
            if not isinstance(val, dict):
                continue
            cleaned_nm: dict[str, list[str]] = {}
            for nt, roles in val.items():
                def_roles = DEFAULT_NOTIFY.get(nt, [])
                if sorted(set(roles)) != sorted(set(def_roles)):
                    cleaned_nm[nt] = list(roles)
            if cleaned_nm:
                out["notify_matrix"] = cleaned_nm
            continue
        if key == "otp" and isinstance(val, dict):
            sub_diff = {k: v for k, v in val.items() if k in default_val and v != default_val[k]}
            if sub_diff:
                out["otp"] = sub_diff
            continue
        if key == "features" and isinstance(val, dict):
            sub_diff = {k: bool(v) for k, v in val.items() if k in default_val and bool(v) != default_val[k]}
            if sub_diff:
                out["features"] = sub_diff
            continue
        if key == "maintenance" and isinstance(val, dict):
            norm = {
                "on": bool(val.get("on", False)),
                "message": str(val.get("message") or "").strip(),
                "message_en": str(val.get("message_en") or "").strip(),
            }
            if norm != default_val:
                out["maintenance"] = norm
            continue
        if key == "names" and isinstance(val, dict):
            pre = val.get("prefixes") if isinstance(val.get("prefixes"), dict) else {}
            cleaned_pre = {
                k: v for k, v in pre.items() if k in DEFAULT_PREFIXES and isinstance(v, str) and v and v != DEFAULT_PREFIXES[k]
            }
            norm_names = {
                "prefixes": cleaned_pre,
                "currency_ar": str(val.get("currency_ar") or DEFAULT_NAMES["currency_ar"]).strip(),
                "currency_en": str(val.get("currency_en") or DEFAULT_NAMES["currency_en"]).strip(),
                "invoice_footer": str(val.get("invoice_footer") or ""),
            }
            if norm_names != DEFAULT_NAMES:
                out["names"] = norm_names
            continue
        if key == "work" and isinstance(val, dict):
            norm_work = {
                "on": bool(val.get("on", False)),
                "days": list(val.get("days")) if isinstance(val.get("days"), list) else list(DEFAULT_WORK["days"]),
                "from": str(val.get("from") or DEFAULT_WORK["from"]),
                "to": str(val.get("to") or DEFAULT_WORK["to"]),
                "holidays": list(val.get("holidays")) if isinstance(val.get("holidays"), list) else [],
            }
            if norm_work != DEFAULT_WORK:
                out["work"] = norm_work
            continue
        if key == "delivery_plan" and isinstance(val, dict):
            norm_dp = {
                "on": bool(val.get("on", False)),
                "cutoff": str(val.get("cutoff") if val.get("cutoff") is not None else DEFAULT_DELIVERY_PLAN["cutoff"]),
                "days_by_area": {
                    str(area): list(days)
                    for area, days in (val.get("days_by_area") or {}).items()
                    if isinstance(days, list) and len(days) > 0
                }
                if isinstance(val.get("days_by_area"), dict)
                else {},
            }
            if norm_dp != DEFAULT_DELIVERY_PLAN:
                out["delivery_plan"] = norm_dp
            continue
        if key == "permissions" and isinstance(val, dict):
            cleaned_perms: dict[str, list[str]] = {}
            for ak, roles in val.items():
                if ak not in ACTIONS or not isinstance(roles, list):
                    continue
                norm_roles = list(dict.fromkeys(roles))
                if "manager" not in norm_roles:
                    norm_roles = ["manager", *norm_roles]
                if sorted(norm_roles) != sorted(ACTIONS[ak]["def"]):
                    cleaned_perms[ak] = norm_roles
            if cleaned_perms:
                out["permissions"] = cleaned_perms
            continue
        if key == "people" and isinstance(val, dict):
            cleaned_people: dict[str, Any] = {}
            for uid_str, rule in val.items():
                if not isinstance(rule, dict):
                    continue
                cleaned_p: dict[str, Any] = {}
                for nf in ("discount_max", "custody_cap", "expense_limit"):
                    if rule.get(nf) is not None:
                        cleaned_p[nf] = rule[nf]
                for lf in ("allow", "deny"):
                    if isinstance(rule.get(lf), list) and len(rule[lf]) > 0:
                        cleaned_p[lf] = list(dict.fromkeys(rule[lf]))
                if cleaned_p:
                    cleaned_people[str(uid_str)] = cleaned_p
            if cleaned_people:
                out["people"] = cleaned_people
            continue
        if key == "warehouses" and isinstance(val, dict):
            cleaned_whs: dict[str, Any] = {}
            for wid_str, wr in val.items():
                if not isinstance(wr, dict):
                    continue
                cleaned_wr: dict[str, Any] = {}
                if wr.get("reservation_hours") is not None:
                    cleaned_wr["reservation_hours"] = wr["reservation_hours"]
                if wr.get("shop_orders") is False:
                    cleaned_wr["shop_orders"] = False
                if wr.get("counter_sale") is False:
                    cleaned_wr["counter_sale"] = False
                if cleaned_wr:
                    cleaned_whs[str(wid_str)] = cleaned_wr
            if cleaned_whs:
                out["warehouses"] = cleaned_whs
            continue
        if val != default_val:
            out[key] = copy.deepcopy(val)
    return out


def _as_num_in_range(
    val: Any,
    lo: float,
    hi: float,
    label_ar: str,
    label_en: str,
) -> tuple[float | None, tuple[str, str] | None]:
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        return None, (f"«{label_ar}» يجب أن يكون بين {lo} و {hi}", f"{label_en} must be between {lo} and {hi}")
    if val < lo or val > hi:
        return None, (f"«{label_ar}» يجب أن يكون بين {lo} و {hi}", f"{label_en} must be between {lo} and {hi}")
    return float(val), None


def _as_int_in_range(
    val: Any,
    lo: int,
    hi: int,
    label_ar: str,
    label_en: str,
    *,
    fallback_none: Any = "__no_fallback__",
) -> tuple[int | None, tuple[str, str] | None]:
    if val is None and fallback_none != "__no_fallback__":
        return int(fallback_none), None
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        return None, (f"«{label_ar}» يجب أن يكون بين {lo} و {hi}", f"{label_en} must be between {lo} and {hi}")
    if isinstance(val, float) and not val.is_integer():
        return None, (f"«{label_ar}» يجب أن يكون رقماً صحيحاً", f"{label_en} must be a whole number")
    num = int(val)
    if num < lo or num > hi:
        return None, (f"«{label_ar}» يجب أن يكون بين {lo} و {hi}", f"{label_en} must be between {lo} and {hi}")
    return num, None


def check_rules(rules: dict[str, Any], tier_ids: set[int] | list[int]) -> dict[str, tuple[str, str]]:
    """Validate normalized `AdvancedRules` dict; keys match frontend `checkRules()`."""
    errors: dict[str, tuple[str, str]] = {}
    valid_tiers = {int(t) for t in tier_ids}

    def num(key: str, val: Any, lo: int, hi: int, ar: str, en: str) -> int | None:
        parsed, err = _as_int_in_range(val, lo, hi, ar, en)
        if err is not None:
            errors[key] = err
        return parsed

    sdm = num("staff_discount_max", rules.get("staff_discount_max"), 0, 100, "سقف خصم الموظف", "Staff discount cap")
    dm = num("discount_max", rules.get("discount_max"), 0, 100, "سقف الخصم الكلي", "Max discount")
    if sdm is not None and dm is not None and dm < sdm:
        errors["discount_max"] = ("سقف الخصم يجب أن يساوي أو يفوق سقف الموظف", "Max discount must be >= staff cap")

    rt = rules.get("round_to")
    if isinstance(rt, bool) or not isinstance(rt, int) or rt not in ROUND_TO_STEPS:
        errors["round_to"] = ("اختر خطوة تقريب صالحة", "Pick a valid rounding step")

    num("offline_max_days", rules.get("offline_max_days"), 0, 30, "أيام الطلب بلا نت", "Offline order days")

    if rules.get("over_limit") not in ("approval", "block", "allow"):
        errors["over_limit"] = ("اختر تصرف سقف الدين", "Pick an over-limit action")

    num("min_shop_order", rules.get("min_shop_order"), 0, 1_000_000_000, "أقل طلب للمحل", "Minimum shop order")
    num("shop_edit_minutes", rules.get("shop_edit_minutes"), 0, 1440, "وقت تعديل المحل", "Shop edit window")

    num("pin_tries", rules.get("pin_tries"), 1, 20, "محاولات الرمز", "PIN attempts")
    dm_list = rules.get("door_methods")
    if not isinstance(dm_list, list) or len(dm_list) == 0 or any(m not in DOOR_METHODS_ALL for m in dm_list):
        errors["door_methods"] = ("اختر طريقة دفع واحدة على الأقل", "Pick at least one payment method")

    num("low_rating_stars", rules.get("low_rating_stars"), 0, 5, "نجوم التقييم المنخفض", "Low-rating threshold")
    num("custody_cap", rules.get("custody_cap"), 0, 1_000_000_000, "حد العهدة", "Custody cap")

    rev_roles = rules.get("reverse_roles")
    if (
        not isinstance(rev_roles, list)
        or len(rev_roles) == 0
        or any(r not in ROLES_ALL for r in rev_roles)
        or "manager" not in rev_roles
    ):
        errors["reverse_roles"] = ("المدير يجب أن يبقى ضمن من يعكسون التسديد", "Manager must stay in reverse roles")

    num("reverse_days", rules.get("reverse_days"), 0, 3650, "عمر التسديد القابل للعكس", "Reverse window")

    tier_id = rules.get("signup_tier_id")
    if tier_id is not None:
        if isinstance(tier_id, bool) or not isinstance(tier_id, int) or tier_id not in valid_tiers:
            errors["signup_tier_id"] = ("نوع سعر غير موجود", "Price tier not found")

    num("signup_debt_limit", rules.get("signup_debt_limit"), 0, 1_000_000_000, "سقف دين المحل الجديد", "New-shop debt limit")
    num("max_shops_per_account", rules.get("max_shops_per_account"), 1, 50, "حد المحلات للحساب", "Shops per account")

    num("visit_near_m", rules.get("visit_near_m"), 20, 5000, "مسافة الزيارة القريبة", "Visit proximity")
    num("reorder_early_days", rules.get("reorder_early_days"), 0, 14, "أيام تذكير إعادة الطلب", "Reorder reminder lead")

    otp = rules.get("otp") if isinstance(rules.get("otp"), dict) else {}
    num("otp.code_minutes", otp.get("code_minutes"), 1, 30, "صلاحية رمز التحقق", "OTP code validity")
    num("otp.resend_seconds", otp.get("resend_seconds"), 10, 600, "انتظار إعادة الإرسال", "OTP resend wait")
    num("otp.per_hour", otp.get("per_hour"), 1, 20, "حد الإرسال بالساعة", "OTP per hour")
    num("otp.tries", otp.get("tries"), 1, 10, "محاولات رمز التحقق", "OTP tries")

    num("login_lock_after", rules.get("login_lock_after"), 0, 20, "حد محاولات الدخول", "Login lock threshold")
    num("login_lock_minutes", rules.get("login_lock_minutes"), 1, 1440, "مدة القفل", "Login lock duration")
    num("password_min", rules.get("password_min"), 4, 32, "أقل طول لكلمة المرور", "Minimum password length")

    ann = rules.get("announcement")
    if isinstance(ann, dict):
        if not str(ann.get("text") or "").strip():
            errors["announcement.text"] = ("اكتب نص الإعلان أو امسح الإعلان", "Write an announcement text or clear it")
        until_raw = ann.get("until")
        if until_raw:
            until_str = str(until_raw).strip()
            valid_d = bool(_DATE_ISO_RE.match(until_str))
            if valid_d:
                try:
                    date.fromisoformat(until_str)
                except ValueError:
                    valid_d = False
            if not valid_d:
                errors["announcement.until"] = ("التاريخ غير صالح (YYYY-MM-DD)", "Invalid date (YYYY-MM-DD)")

    nm = rules.get("notify_matrix")
    if isinstance(nm, dict):
        for nt, roles in nm.items():
            if not isinstance(roles, list) or any(r not in ROLES_ALL for r in roles):
                errors[f"notify_matrix.{nt}"] = ("قائمة أدوار غير صالحة", "Invalid roles list")
    elif nm is not None:
        errors["notify_matrix"] = ("صيغة مصفوفة الإشعارات غير صالحة", "Invalid notify matrix")

    # ---- Round 2: permissions, people, role limits, work, delivery_plan, warehouses, names ----
    max_disc = float(dm) if dm is not None else float(DEFAULT_RULES["discount_max"])
    action_keys = set(ACTIONS.keys())

    perms = rules.get("permissions")
    if isinstance(perms, dict):
        for ak, roles in perms.items():
            if ak not in action_keys or not isinstance(roles, list) or any(r not in STAFF_ROLES for r in roles):
                errors[f"permissions.{ak}"] = ("وظائف غير صحيحة", "Invalid roles")
    elif perms is not None:
        errors["permissions"] = ("صيغة الصلاحيات غير صالحة", "Invalid permissions format")

    people = rules.get("people")
    if isinstance(people, dict):
        for uid, p in people.items():
            if not isinstance(p, dict):
                errors[f"people.{uid}"] = ("صيغة غير صالحة", "Invalid person rule")
                continue
            if p.get("discount_max") is not None:
                _, err = _as_num_in_range(p.get("discount_max"), 0, max_disc, "خصم الشخص", "Person discount")
                if err is not None:
                    errors[f"people.{uid}.discount_max"] = ("من 0 إلى أعلى خصم", f"Between 0 and {max_disc}%")
            if p.get("custody_cap") is not None:
                num(f"people.{uid}.custody_cap", p.get("custody_cap"), 0, 1_000_000_000, "عهدة الشخص", "Person custody cap")
            if p.get("expense_limit") is not None:
                num(f"people.{uid}.expense_limit", p.get("expense_limit"), 0, 1_000_000_000, "سقف صرف الشخص", "Person expense limit")
            allow = p.get("allow")
            deny = p.get("deny")
            if (allow is not None and (not isinstance(allow, list) or any(k not in action_keys for k in allow))) or (
                deny is not None and (not isinstance(deny, list) or any(k not in action_keys for k in deny))
            ):
                errors[f"people.{uid}.actions"] = ("إجراء غير معروف", "Unknown action")

    dbr = rules.get("discount_by_role")
    if isinstance(dbr, dict):
        for role, v in dbr.items():
            if role not in STAFF_ROLES:
                errors[f"discount_by_role.{role}"] = ("وظيفة غير صحيحة", "Invalid role")
                continue
            _, err = _as_num_in_range(v, 0, max_disc, "خصم الوظيفة", "Role discount")
            if err is not None:
                errors[f"discount_by_role.{role}"] = ("من 0 إلى أعلى خصم", f"Between 0 and {max_disc}%")

    ebr = rules.get("expense_by_role")
    if isinstance(ebr, dict):
        for role, v in ebr.items():
            if role not in STAFF_ROLES:
                errors[f"expense_by_role.{role}"] = ("وظيفة غير صحيحة", "Invalid role")
                continue
            num(f"expense_by_role.{role}", v, 0, 1_000_000_000, "سقف صرف الوظيفة", "Role expense limit")

    w = rules.get("work") if isinstance(rules.get("work"), dict) else {}
    w_days = w.get("days")
    if not isinstance(w_days, list) or any(
        isinstance(x, bool) or not isinstance(x, int) or x < 0 or x > 6 for x in w_days
    ):
        errors["work.days"] = ("أيام غير صحيحة", "Invalid days")
    elif w.get("on") and len(w_days) == 0:
        errors["work.days"] = ("اختر يوم عمل واحداً على الأقل", "Select at least one work day")

    w_from = str(w.get("from") or "")
    w_to = str(w.get("to") or "")
    if not _TIME_HHMM_RE.match(w_from) or not _TIME_HHMM_RE.match(w_to) or _hhmm_to_minutes(w_from) >= _hhmm_to_minutes(w_to):
        errors["work.hours"] = ("البداية قبل النهاية (00:00 – 23:59)", "Start must be before end (00:00 - 23:59)")

    holidays = w.get("holidays")
    if not isinstance(holidays, list):
        errors["work.holidays"] = ("كل عطلة بتاريخ واسم", "Each holiday needs date and name")
    else:
        for h in holidays:
            if not isinstance(h, dict):
                errors["work.holidays"] = ("كل عطلة بتاريخ واسم", "Each holiday needs date and name")
                break
            hd = str(h.get("date") or "")
            hn = str(h.get("name") or "").strip()
            valid_hd = bool(_DATE_ISO_RE.match(hd))
            if valid_hd:
                try:
                    date.fromisoformat(hd)
                except ValueError:
                    valid_hd = False
            if not valid_hd or not hn:
                errors["work.holidays"] = ("كل عطلة بتاريخ واسم", "Each holiday needs date and name")
                break

    dp = rules.get("delivery_plan") if isinstance(rules.get("delivery_plan"), dict) else {}
    dp_cutoff = str(dp.get("cutoff") or "")
    if dp_cutoff and not _TIME_HHMM_RE.match(dp_cutoff):
        errors["delivery_plan.cutoff"] = ("وقت بصيغة 14:00", "Time format HH:MM")
    dba = dp.get("days_by_area")
    if isinstance(dba, dict):
        for area, adays in dba.items():
            if not isinstance(adays, list) or any(
                isinstance(x, bool) or not isinstance(x, int) or x < 0 or x > 6 for x in adays
            ):
                errors[f"delivery_plan.days_by_area.{area}"] = ("أيام غير صحيحة", "Invalid days")

    whs = rules.get("warehouses")
    if isinstance(whs, dict):
        for wid, wr in whs.items():
            if isinstance(wr, dict) and wr.get("reservation_hours") is not None:
                num(
                    f"warehouses.{wid}.reservation_hours",
                    wr.get("reservation_hours"),
                    1,
                    72,
                    "ساعات حجز المخزن",
                    "Warehouse reservation hours",
                )

    names = rules.get("names") if isinstance(rules.get("names"), dict) else {}
    prefixes = names.get("prefixes") if isinstance(names.get("prefixes"), dict) else {}
    for pk, pv in prefixes.items():
        if pk not in DEFAULT_PREFIXES or not isinstance(pv, str) or not _PREFIX_RE.match(pv):
            errors[f"names.prefixes.{pk}"] = ("من 1 إلى 6 أحرف إنجليزية كبيرة", "1 to 6 uppercase English letters")
    effective_prefixes = [prefix_from_rules(rules, k) for k in DEFAULT_PREFIXES]
    if len(set(effective_prefixes)) != len(effective_prefixes):
        errors["names.prefixes"] = ("لكل نوع بادئة مختلفة", "Every document kind must have a unique prefix")
    cur_ar = str(names.get("currency_ar") or "").strip()
    cur_en = str(names.get("currency_en") or "").strip()
    if not cur_ar or len(cur_ar) > 10 or not cur_en or len(cur_en) > 10:
        errors["names.currency"] = ("اسم العملة (حتى 10 أحرف)", "Currency name (up to 10 chars)")
    footer = str(names.get("invoice_footer") or "")
    if len(footer) > 200:
        errors["names.invoice_footer"] = ("حتى 200 حرف", "Up to 200 chars")

    return errors


def flatten_settings_for_history(settings_dict: dict[str, Any]) -> dict[str, Any]:
    """Flatten `Settings` dictionary into comparable key -> value map matching `mock.ts`."""
    flat: dict[str, Any] = {}
    for k, v in settings_dict.items():
        if k == "advanced":
            continue
        flat[k] = v

    adv = settings_dict.get("advanced") if isinstance(settings_dict.get("advanced"), dict) else {}
    for ak, av in adv.items():
        if ak == "auto_confirm_rep_orders":
            continue
        if ak == "announcement":
            flat["advanced.announcement"] = av.get("text") if isinstance(av, dict) else None
        elif isinstance(av, dict):
            for sk, sv in av.items():
                flat[f"advanced.{ak}.{sk}"] = sv
        else:
            flat[f"advanced.{ak}"] = av
    return flat


def diff_settings_changes(before_dict: dict[str, Any], after_dict: dict[str, Any]) -> list[dict[str, Any]]:
    """Produce `[{key, from, to}, ...]` excluding `auto_confirm_rep_orders` duplicate."""
    b_flat = flatten_settings_for_history(before_dict)
    a_flat = flatten_settings_for_history(after_dict)
    changes: list[dict[str, Any]] = []
    seen_keys: list[str] = list(b_flat.keys())
    for k in a_flat:
        if k not in b_flat:
            seen_keys.append(k)
    for k in seen_keys:
        fv = b_flat.get(k)
        tv = a_flat.get(k)
        if fv != tv:
            changes.append({"key": k, "from": fv, "to": tv})
    return changes


def round_amount_by_rule(amount: int | Decimal, setting: Any = None) -> int:
    """Round a total using active `round_to` advanced rule (fallback 250)."""
    r = rules_of(setting)
    step = int(r.get("round_to", 250))
    return round_by(amount, step)
