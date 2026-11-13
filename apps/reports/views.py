"""DRF views for Dashboard and Reports."""
from __future__ import annotations

from drf_spectacular.utils import extend_schema
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.views import APIView

from apps.accounts.models import Role
from apps.accounts.permissions import role_permission
from apps.common.responses import ok_response
from apps.reports.serializers import RatingsReportSerializer
from apps.reports.services import (
    delivery_ratings_report,
    get_daily_sales,
    get_dashboard,
    get_debts_report,
    get_near_expiry_report,
    get_rep_targets,
    get_sales_report,
    get_slow_moving_report,
)


def _opt_int(val: str | None) -> int | None:
    if not val:
        return None
    try:
        return int(val)
    except ValueError:
        return None


class DashboardView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request: Request):
        return ok_response(data=get_dashboard(actor=request.user, warehouse_id=_opt_int(request.query_params.get("warehouse_id"))))


class DailySalesReportView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request: Request):
        return ok_response(
            data=get_daily_sales(
                actor=request.user,
                date_str=request.query_params.get("date"),
                warehouse_id=_opt_int(request.query_params.get("warehouse_id")),
            )
        )


class SalesReportView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request: Request):
        return ok_response(
            data=get_sales_report(
                actor=request.user,
                group_by=request.query_params.get("group_by"),
                date_from=request.query_params.get("from") or None,
                date_to=request.query_params.get("to") or None,
                warehouse_id=_opt_int(request.query_params.get("warehouse_id")),
            )
        )


class SlowMovingReportView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request: Request):
        days = _opt_int(request.query_params.get("days")) or 30
        return ok_response(
            data=get_slow_moving_report(
                actor=request.user,
                days=days,
                warehouse_id=_opt_int(request.query_params.get("warehouse_id")),
            )
        )


class NearExpiryReportView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request: Request):
        return ok_response(
            data=get_near_expiry_report(
                actor=request.user,
                days=_opt_int(request.query_params.get("days")),
                warehouse_id=_opt_int(request.query_params.get("warehouse_id")),
            )
        )


class DebtsReportView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request: Request):
        return ok_response(data=get_debts_report(actor=request.user))


class RepTargetsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request: Request):
        return ok_response(
            data=get_rep_targets(
                actor=request.user,
                month=request.query_params.get("month") or None,
            )
        )


class DeliveryRatingsReportView(APIView):
    permission_classes = [role_permission(Role.MANAGER, Role.ACCOUNTANT)]

    @extend_schema(
        summary="Customer ratings report for completed deliveries",
        responses={200: RatingsReportSerializer},
    )
    def get(self, request: Request):
        return ok_response(data=delivery_ratings_report(actor=request.user))

