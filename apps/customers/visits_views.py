"""DRF views for Visits («خط السير»)."""
from __future__ import annotations

from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import serializers
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.views import APIView

from apps.common.responses import ok_response
from apps.customers.visits_services import (
    build_visits_report,
    list_visits_for_actor,
    record_visit,
    set_customer_visit_days,
)


class VisitInputSerializer(serializers.Serializer):
    customer_id = serializers.IntegerField()
    outcome = serializers.ChoiceField(choices=["order", "payment", "no_order", "closed"])
    reason = serializers.ChoiceField(choices=["stocked", "no_money", "price", "other"], required=False)
    note = serializers.CharField(required=False, allow_blank=True)
    lat = serializers.FloatField(required=False, allow_null=True)
    lng = serializers.FloatField(required=False, allow_null=True)


class VisitDaysInputSerializer(serializers.Serializer):
    days = serializers.ListField(child=serializers.IntegerField(min_value=0, max_value=6))


class VisitListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="List visits for a day",
        parameters=[
            OpenApiParameter("day", str, OpenApiParameter.QUERY, required=False),
            OpenApiParameter("rep_id", int, OpenApiParameter.QUERY, required=False),
        ],
        responses={200: serializers.ListField(child=serializers.DictField())},
    )
    def get(self, request: Request):
        return ok_response(
            list_visits_for_actor(
                actor=request.user,
                day_str=request.query_params.get("day"),
                rep_id_raw=request.query_params.get("rep_id"),
            )
        )

    @extend_schema(
        summary="Record a rep shop visit",
        request=VisitInputSerializer,
        responses={200: serializers.DictField()},
    )
    def post(self, request: Request):
        return ok_response(record_visit(actor=request.user, data=request.data))


class CustomerVisitDaysView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="Set weekly planned visit days for a customer",
        request=VisitDaysInputSerializer,
        responses={200: serializers.DictField()},
    )
    def post(self, request: Request, customer_id: int):
        return ok_response(
            set_customer_visit_days(
                actor=request.user,
                customer_id=customer_id,
                raw_days=request.data.get("days"),
            )
        )


class VisitsReportView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="Visits compliance report",
        parameters=[
            OpenApiParameter("from", str, OpenApiParameter.QUERY, required=False),
            OpenApiParameter("to", str, OpenApiParameter.QUERY, required=False),
        ],
        responses={200: serializers.DictField()},
    )
    def get(self, request: Request):
        return ok_response(
            build_visits_report(
                actor=request.user,
                from_str=request.query_params.get("from"),
                to_str=request.query_params.get("to"),
            )
        )
