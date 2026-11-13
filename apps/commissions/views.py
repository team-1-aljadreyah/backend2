"""DRF views and URL routes for Commissions."""
from __future__ import annotations

from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.views import APIView

from apps.commissions.services import (
    close_commission_month,
    list_closed_months,
    list_commission_lines,
    list_commissions,
)
from apps.common.responses import ok_response


class CommissionListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request: Request):
        return ok_response(data=list_commissions(actor=request.user, month_raw=request.query_params.get("month")))


class CommissionCloseView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request: Request):
        return ok_response(data=close_commission_month(actor=request.user, month_raw=request.data.get("month")))


class CommissionClosedListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request: Request):
        return ok_response(data=list_closed_months(actor=request.user))


class CommissionLinesView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request: Request):
        raw_rep = request.query_params.get("rep_id")
        try:
            rep_id = int(raw_rep) if raw_rep else None
        except ValueError:
            rep_id = None
        return ok_response(
            data=list_commission_lines(
                actor=request.user,
                month_raw=request.query_params.get("month"),
                rep_id=rep_id,
            )
        )
