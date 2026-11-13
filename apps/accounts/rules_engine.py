"""Automation rules engine evaluating business triggers (`order_created`, `payment_created`, etc.)."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from apps.accounts.models import Role, RuleRun, SiteConfig
from apps.notifications.services import notify_role


def _to_decimal(val: Any) -> Decimal | None:
    try:
        return Decimal(str(val))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _matches_condition(actual: Any, op: str, expected: Any) -> bool:
    if op in ("gt", "gte", "lt"):
        a = _to_decimal(actual)
        b = _to_decimal(expected)
        if a is None or b is None:
            return False
        if op == "gt":
            return a > b
        if op == "gte":
            return a >= b
        return a < b
    if op == "eq":
        return str(actual) == str(expected)
    if op == "neq":
        return str(actual) != str(expected)
    return False


def run_automation_rules(
    trigger: str,
    values: dict[str, Any],
    *,
    number: str,
    link: str,
    by: str,
    customer: str = "",
) -> list[str]:
    """Evaluate active automation rules matching `trigger` and persist execution logs."""
    cfg = SiteConfig.objects.only("rules").filter(pk=1).first()
    rules = cfg.rules if cfg and isinstance(cfg.rules, list) else []
    approvals: list[str] = []

    for rule in rules:
        if not isinstance(rule, dict) or not rule.get("active") or rule.get("trigger") != trigger:
            continue
        conditions = rule.get("conditions") or []
        if not all(
            isinstance(c, dict)
            and c.get("field") in values
            and _matches_condition(values[c["field"]], str(c.get("op") or ""), c.get("value"))
            for c in conditions
        ):
            continue

        amount = int(_to_decimal(values.get("total") or values.get("amount") or 0) or 0)
        template = str(rule.get("message") or "").strip() or "{number}: {amount} د.ع"
        text = (
            template.replace("{number}", number)
            .replace("{amount}", f"{amount:,}")
            .replace("{customer}", customer)
            .replace("{by}", by)
        )
        wants_approval = bool(rule.get("approval")) and trigger in ("order_created", "expense_created")
        notify_roles = list(dict.fromkeys([*(rule.get("notify") or []), *([Role.MANAGER] if wants_approval else [])]))

        rule_name = str(rule.get("name") or rule.get("id") or "Rule")
        ref_parts = [p for p in link.strip("/").split("/") if p]
        ref_type = ref_parts[0].rstrip("s") if ref_parts else ""
        ref_id = ref_parts[1] if len(ref_parts) > 1 and ref_parts[1].isdigit() else ""
        for role in notify_roles:
            notify_role(
                role=role,
                kind="needs_approval" if wants_approval else "rule",
                title_ar=f"قاعدة «{rule_name}»: {number} ينتظر موافقتك" if wants_approval else f"قاعدة «{rule_name}»",
                title_en=f"Rule {rule_name}: {number} waits for you" if wants_approval else f"Rule {rule_name}",
                body_ar=text,
                body_en=text,
                ref_type=ref_type,
                ref_id=ref_id,
            )

        if wants_approval:
            approvals.append(rule_name)

        did_parts = []
        if notify_roles:
            did_parts.append(f"إشعار إلى {len(notify_roles)} وظيفة")
        if wants_approval:
            did_parts.append("ينتظر موافقة المدير")

        RuleRun.objects.create(
            rule_id=str(rule.get("id") or ""),
            rule_name=rule_name[:160],
            trigger=trigger[:40],
            record=number[:120],
            link=link[:255],
            did=" · ".join(did_parts)[:255] or "تم التنفيذ",
        )

    return approvals


def list_recent_rule_runs(limit: int = 100) -> list[dict[str, Any]]:
    rows = RuleRun.objects.order_by("-id")[:limit]
    return [
        {
            "id": r.id,
            "rule_id": r.rule_id,
            "rule": r.rule_name,
            "at": r.created_at.isoformat(),
            "record": r.record,
            "link": r.link,
            "did": r.did,
        }
        for r in rows
    ]
