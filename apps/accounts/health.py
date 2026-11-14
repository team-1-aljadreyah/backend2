"""Security & operational health diagnostics for GET /api/v1/setup/health."""
from __future__ import annotations

import re
from datetime import timedelta
from typing import Any

from django.utils import timezone

from apps.accounts.models import LoginAttempt, Role, SiteConfig, SystemSetting, User

_WEAK_PASSWORDS = frozenset({"123456", "12345678", "password", "000000"})
_REPEATED_DIGIT_RE = re.compile(r"^(\d)\1+$")
DEFAULT_EXPENSE_APPROVAL_ABOVE = 1_000_000


def is_weak_password(password: str, phone: str = "") -> bool:
    if len(password) < 8:
        return True
    if _REPEATED_DIGIT_RE.match(password):
        return True
    if password in _WEAK_PASSWORDS:
        return True
    if phone and password == phone:
        return True
    return False


def build_health_report() -> dict[str, Any]:
    now = timezone.now()
    day_ago = now - timedelta(days=1)
    idle_cutoff = now - timedelta(days=30)

    attempts_24h = list(LoginAttempt.objects.filter(created_at__gte=day_ago).select_related("user").order_by("id"))
    failed_24h = [a for a in attempts_24h if not a.ok]
    reasons: dict[str, int] = {"wrong": 0, "stopped": 0, "hours": 0, "locked": 0}
    for f in failed_24h:
        r = f.reason if f.reason in reasons else "wrong"
        reasons[r] += 1

    live_users = list(User.objects.filter(deleted=False).order_by("id"))
    active_users = [u for u in live_users if u.active]

    def _person(u: User) -> dict[str, Any]:
        return {"id": u.id, "name": u.name, "role": u.role}

    never: list[dict[str, Any]] = []
    idle: list[dict[str, Any]] = []
    weak: list[dict[str, Any]] = []
    by_role: dict[str, int] = {r: 0 for r in Role.values}

    for u in active_users:
        if u.role in by_role:
            by_role[u.role] += 1
        if u.last_login is None:
            never.append(_person(u))
        elif u.last_login < idle_cutoff:
            idle.append({**_person(u), "last": u.last_login.isoformat()})
        if u.weak_password:
            weak.append(_person(u))

    managers = by_role[Role.MANAGER]
    total_24h = len(attempts_24h)
    rate = (len(failed_24h) / total_24h) if total_24h else 0.0

    setting = SystemSetting.get_solo()
    limit = (
        setting.expense_approval_above
        if setting.expense_approval_above is not None
        else DEFAULT_EXPENSE_APPROVAL_ABOVE
    )
    cfg = SiteConfig.objects.only("profiles").filter(pk=1).first()
    profiles = cfg.profiles if cfg and isinstance(cfg.profiles, list) else []

    checks = [
        {
            "key": "passwords",
            "ok": len(weak) == 0,
            "weight": 30,
            "title": "كلمات مرور قوية",
            "title_en": "Strong passwords",
            "detail": (
                f"{len(weak)} حساب بكلمة مرور سهلة التخمين (مثل 123456 أو رقم الهاتف). غيّرها من صفحة المستخدمين."
                if weak
                else "لا أحد يستعمل كلمة مرور سهلة."
            ),
            "detail_en": f"{len(weak)} accounts use an easy password." if weak else "Nobody uses an easy password.",
            "fix": "/users",
        },
        {
            "key": "managers",
            "ok": managers <= 2,
            "weight": 15,
            "title": "عدد قليل من المدراء",
            "title_en": "Few managers",
            "detail": (
                f"{managers} حساب مدير فقط."
                if managers <= 2
                else f"{managers} حسابات مدير: المدير يرى ويغيّر كل شيء، فاجعلهم أقل ما يمكن."
            ),
            "detail_en": f"{managers} manager accounts.",
            "fix": "/users",
        },
        {
            "key": "logins",
            "ok": rate < 0.2 and len(failed_24h) < 10,
            "weight": 20,
            "title": "محاولات دخول فاشلة قليلة",
            "title_en": "Few failed logins",
            "detail": (
                f"{len(failed_24h)} محاولة فاشلة من {total_24h} في آخر 24 ساعة."
                if failed_24h
                else "لا محاولات فاشلة في آخر 24 ساعة."
            ),
            "detail_en": f"{len(failed_24h)} failed of {total_24h} in 24 hours.",
            "fix": "/settings/advanced?s=health",
        },
        {
            "key": "idle",
            "ok": len(idle) == 0,
            "weight": 15,
            "title": "لا حسابات خاملة",
            "title_en": "No idle accounts",
            "detail": (
                f"{len(idle)} حساب فعّال لم يدخل منذ أكثر من 30 يوماً: أوقفه إن لم يعد يعمل."
                if idle
                else "كل الحسابات الفعّالة مستعملة."
            ),
            "detail_en": f"{len(idle)} active accounts unused for 30+ days.",
            "fix": "/users",
        },
        {
            "key": "expenses",
            "ok": 0 < limit <= 5_000_000,
            "weight": 10,
            "title": "حد موافقة الصرفيات",
            "title_en": "Expense approval limit",
            "detail": f"الصرفيات فوق {limit:,} د.ع تنتظر موافقتك" + ("، والحد مرتفع." if limit > 5_000_000 else "."),
            "detail_en": f"Expenses above {limit} IQD wait for you.",
            "fix": "/settings",
        },
        {
            "key": "profiles",
            "ok": len(profiles) > 0,
            "weight": 10,
            "title": "صلاحيات على قدر العمل",
            "title_en": "Access fits the work",
            "detail": (
                f"{len(profiles)} ملف صلاحيات مستعمل."
                if profiles
                else "لا ملفات صلاحيات بعد: كل شخص يرى كل ما تسمح به وظيفته."
            ),
            "detail_en": f"{len(profiles)} permission profiles.",
            "fix": "/settings/advanced?s=profiles",
        },
    ]

    total_weight = sum(c["weight"] for c in checks)
    passed_weight = sum(c["weight"] for c in checks if c["ok"])
    score = round((passed_weight / total_weight) * 100) if total_weight else 100

    recent_failed = [
        {
            "phone": f.phone,
            **({"name": f.user.name} if f.user else {}),
            "reason": f.reason if f.reason in reasons else "wrong",
            "at": f.created_at.isoformat(),
        }
        for f in reversed(failed_24h[-10:])
    ]

    return {
        "logins": {
            "ok": total_24h - len(failed_24h),
            "failed": len(failed_24h),
            "reasons": reasons,
            "recent": recent_failed,
        },
        "score": score,
        "checks": checks,
        "users": {
            "active": len(active_users),
            "stopped": len(live_users) - len(active_users),
            "managers": managers,
            "in_profiles": sum(1 for u in active_users if u.profile_id),
            "by_role": by_role,
            "never": never,
            "idle": idle,
            "weak": weak,
        },
    }
