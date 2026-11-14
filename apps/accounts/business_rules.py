"""Advanced business rules («قواعد العمل») defaults, validation, diffing, and helpers.

Mirrors `frontend/lib/api/advanced.ts` and `frontend/docs/backend.md` Section 3.6.
"""
from __future__ import annotations

import copy
import re
from datetime import date
from decimal import Decimal
from typing import Any

from apps.common.utils import round_by

ROLES_ALL: tuple[str, ...] = ("manager", "accountant", "warehouse", "rep", "staff", "shop")
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

# Map backend-specific notification `kind` strings to canonical `NotificationType` keys.
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
            base["names"] = {
                "prefixes": copy.deepcopy(val.get("prefixes")) if isinstance(val.get("prefixes"), dict) else {},
                "currency_ar": str(val.get("currency_ar") or DEFAULT_NAMES["currency_ar"]),
                "currency_en": str(val.get("currency_en") or DEFAULT_NAMES["currency_en"]),
                "invoice_footer": str(val.get("invoice_footer") or ""),
            }
        elif key == "work" and isinstance(val, dict):
            base["work"] = {
                "on": bool(val.get("on", False)),
                "days": list(val.get("days")) if isinstance(val.get("days"), list) else list(DEFAULT_WORK["days"]),
                "from": str(val.get("from") or DEFAULT_WORK["from"]),
                "to": str(val.get("to") or DEFAULT_WORK["to"]),
                "holidays": list(val.get("holidays")) if isinstance(val.get("holidays"), list) else [],
            }
        elif key == "delivery_plan" and isinstance(val, dict):
            base["delivery_plan"] = {
                "on": bool(val.get("on", False)),
                "cutoff": str(val.get("cutoff") or DEFAULT_DELIVERY_PLAN["cutoff"]),
                "days_by_area": copy.deepcopy(val.get("days_by_area")) if isinstance(val.get("days_by_area"), dict) else {},
            }
        elif key == "announcement":
            if isinstance(val, dict):
                base["announcement"] = copy.deepcopy(val)
            else:
                base["announcement"] = None
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

    # Top-level `auto_confirm_rep_orders` on SystemSetting is canonical if not explicitly passed differently
    if isinstance(setting_or_advanced, dict):
        if "auto_confirm_rep_orders" in raw_adv:
            base["auto_confirm_rep_orders"] = bool(raw_adv["auto_confirm_rep_orders"])
    else:
        base["auto_confirm_rep_orders"] = auto_confirm
    return base


def public_rules(rules: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return unauthenticated `site().live` rules slice matching `publicRules()`."""
    r = rules if rules is not None else rules_of()
    names = r.get("names") if isinstance(r.get("names"), dict) else {}
    perms = r.get("permissions") if isinstance(r.get("permissions"), dict) else {}
    manage_roles = list(perms["manage_users"]) if isinstance(perms.get("manage_users"), list) else ["manager"]
    if "manager" not in manage_roles:
        manage_roles = ["manager", *manage_roles]
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
        if val != default_val:
            out[key] = copy.deepcopy(val)
    return out


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

    sdm = num("staff_discount_max", rules.get("staff_discount_max"), 0, 30, "سقف خصم الموظف", "Staff discount cap")
    dm = num("discount_max", rules.get("discount_max"), 0, 90, "سقف الخصم الكلي", "Max discount")
    if sdm is not None and dm is not None and dm < sdm:
        errors["discount_max"] = ("سقف الخصم يجب أن يساوي أو يفوق سقف الموظف", "Max discount must be >= staff cap")

    rt = rules.get("round_to")
    if isinstance(rt, bool) or not isinstance(rt, int) or rt not in ROUND_TO_STEPS:
        errors["round_to"] = ("اختر خطوة تقريب صالحة", "Pick a valid rounding step")

    num("offline_max_days", rules.get("offline_max_days"), 1, 30, "أيام الطلب بلا نت", "Offline order days")

    if rules.get("over_limit") not in ("approval", "block", "allow"):
        errors["over_limit"] = ("اختر تصرف سقف الدين", "Pick an over-limit action")

    num("min_shop_order", rules.get("min_shop_order"), 0, 50_000_000, "أقل طلب للمحل", "Minimum shop order")
    num("shop_edit_minutes", rules.get("shop_edit_minutes"), 0, 1440, "وقت تعديل المحل", "Shop edit window")

    num("pin_tries", rules.get("pin_tries"), 1, 20, "محاولات الرمز", "PIN attempts")
    dm_list = rules.get("door_methods")
    if not isinstance(dm_list, list) or len(dm_list) == 0 or any(m not in DOOR_METHODS_ALL for m in dm_list):
        errors["door_methods"] = ("اختر طريقة دفع واحدة على الأقل", "Pick at least one payment method")

    num("low_rating_stars", rules.get("low_rating_stars"), 0, 5, "نجوم التقييم المنخفض", "Low-rating threshold")
    num("custody_cap", rules.get("custody_cap"), 0, 500_000_000, "حد العهدة", "Custody cap")

    rev_roles = rules.get("reverse_roles")
    if (
        not isinstance(rev_roles, list)
        or len(rev_roles) == 0
        or any(r not in ROLES_ALL for r in rev_roles)
        or "manager" not in rev_roles
    ):
        errors["reverse_roles"] = ("المدير يجب أن يبقى ضمن من يعكسون التسديد", "Manager must stay in reverse roles")

    num("reverse_days", rules.get("reverse_days"), 0, 365, "عمر التسديد القابل للعكس", "Reverse window")

    tier_id = rules.get("signup_tier_id")
    if tier_id is not None:
        if isinstance(tier_id, bool) or not isinstance(tier_id, int) or tier_id not in valid_tiers:
            errors["signup_tier_id"] = ("نوع سعر غير موجود", "Price tier not found")

    num("signup_debt_limit", rules.get("signup_debt_limit"), 0, 500_000_000, "سقف دين المحل الجديد", "New-shop debt limit")
    num("max_shops_per_account", rules.get("max_shops_per_account"), 1, 50, "حد المحلات للحساب", "Shops per account")

    num("visit_near_m", rules.get("visit_near_m"), 20, 2000, "مسافة الزيارة القريبة", "Visit proximity")
    num("reorder_early_days", rules.get("reorder_early_days"), 0, 14, "أيام تذكير إعادة الطلب", "Reorder reminder lead")

    otp = rules.get("otp") if isinstance(rules.get("otp"), dict) else {}
    num("otp.code_minutes", otp.get("code_minutes"), 1, 30, "صلاحية رمز التحقق", "OTP code validity")
    num("otp.resend_seconds", otp.get("resend_seconds"), 15, 300, "انتظار إعادة الإرسال", "OTP resend wait")
    num("otp.per_hour", otp.get("per_hour"), 1, 20, "حد الإرسال بالساعة", "OTP per hour")
    num("otp.tries", otp.get("tries"), 1, 20, "محاولات رمز التحقق", "OTP tries")

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
