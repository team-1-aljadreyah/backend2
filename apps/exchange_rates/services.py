"""USD exchange rate services, views, and URLs."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from django.db import transaction
from rest_framework import status as http_status
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.views import APIView

from apps.accounts.models import Role, SystemSetting, User
from apps.audit.services import record_audit
from apps.common.exceptions import ApiException, ErrorCode
from apps.common.responses import ok_response
from apps.common.utils import baghdad_today
from apps.exchange_rates.models import UsdRate
from apps.notifications.services import notify_role


def serialize_usd_rate(obj: UsdRate) -> dict[str, Any]:
    val = int(obj.rate) if obj.rate == int(obj.rate) else float(obj.rate)
    return {
        "date": obj.date.isoformat(),
        "rate": val,
        "at": obj.at.isoformat(),
        "by": {
            "id": obj.by_id,
            "name": obj.by.name,
        },
    }


def list_usd_rates() -> list[dict[str, Any]]:
    return [serialize_usd_rate(r) for r in UsdRate.objects.select_related("by").order_by("-date", "-at")[:60]]


@transaction.atomic
def set_today_usd_rate(*, actor: User, raw_rate: Any) -> dict[str, Any]:
    if actor.role not in (Role.MANAGER, Role.ACCOUNTANT):
        raise ApiException(code=ErrorCode.FORBIDDEN, status_code=http_status.HTTP_403_FORBIDDEN)
    try:
        rate = Decimal(str(raw_rate))
        if rate <= 0:
            raise ValueError
    except (InvalidOperation, ValueError) as exc:
        msg = ("سعر الصرف يجب أن يكون أكبر من صفر", "Exchange rate must be positive")
        raise ApiException(code=ErrorCode.VALIDATION, message=msg, errors={"rate": msg}) from exc

    today = baghdad_today()
    obj, _ = UsdRate.objects.update_or_create(
        date=today,
        defaults={"rate": rate, "by": actor},
    )
    setting = SystemSetting.get_solo()
    setting.usd_rate = rate
    setting.save(update_fields=["usd_rate", "updated_at"])

    val_display = f"{int(rate):,}" if rate == int(rate) else str(rate)
    for role in (Role.MANAGER, Role.ACCOUNTANT, Role.REP):
        notify_role(
            role=role,
            kind="usd_rate",
            title_ar=f"تحديث سعر الصرف اليومي: 1$ = {val_display} د.ع",
            title_en=f"USD rate updated: $1 = {val_display} IQD",
            body_ar=f"تم التثبيت بواسطة {actor.name}",
            body_en=f"Updated by {actor.name}",
            ref_type="settings",
            ref_id="usd_rate",
        )
    record_audit(
        actor=actor,
        action="update",
        entity_type="settings",
        entity_id="usd_rate",
        summary=f"Set USD rate to {val_display} IQD",
        summary_ar=f"تثبيت سعر الصرف {val_display} د.ع",
        after={"date": today.isoformat(), "rate": str(rate)},
    )
    refreshed = UsdRate.objects.select_related("by").get(id=obj.id)
    return serialize_usd_rate(refreshed)


class UsdRateListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request: Request):
        return ok_response(data=list_usd_rates())

    def post(self, request: Request):
        return ok_response(data=set_today_usd_rate(actor=request.user, raw_rate=request.data.get("rate")))
