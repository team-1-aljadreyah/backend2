"""DRF views for Reorder Reminders and Day Summary."""
from __future__ import annotations

from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import serializers
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.views import APIView

from apps.common.responses import ok_response
from apps.reports.extra_services import build_day_summary, get_late_shops, get_my_reorder_due


class ReorderDueView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(summary="Get items due for reorder for active shop", responses={200: serializers.ListField(child=serializers.DictField())})
    def get(self, request: Request):
        return ok_response(get_my_reorder_due(actor=request.user))


class LateShopsView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(summary="Get shops overdue for next order", responses={200: serializers.ListField(child=serializers.DictField())})
    def get(self, request: Request):
        return ok_response(get_late_shops(actor=request.user))


class DaySummaryReportView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="Get daily summary compared to yesterday and last week",
        parameters=[OpenApiParameter("date", str, OpenApiParameter.QUERY, required=False)],
        responses={200: serializers.DictField()},
    )
    def get(self, request: Request):
        return ok_response(build_day_summary(actor=request.user, date_str=request.query_params.get("date")))
