"""DRF views for Stock Alerts, Clearances, and Dated Prices."""
from __future__ import annotations

from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.views import APIView

from apps.catalog.features_services import (
    cancel_clearance,
    cancel_scheduled_price,
    create_scheduled_price,
    get_item_price_history,
    get_my_stock_alerts,
    get_stock_waitlist,
    list_clearances_for_actor,
    list_scheduled_prices_for_actor,
    set_stock_alert,
    start_clearance,
)
from apps.common.responses import ok_response


class StockAlertInputSerializer(serializers.Serializer):
    item_id = serializers.IntegerField()
    on = serializers.BooleanField()


class ClearanceInputSerializer(serializers.Serializer):
    batch_id = serializers.IntegerField()
    percent = serializers.IntegerField(min_value=5, max_value=70)


class ScheduledPriceInputSerializer(serializers.Serializer):
    item_ids = serializers.ListField(child=serializers.IntegerField())
    start = serializers.DateField()
    percent = serializers.FloatField(required=False)
    prices = serializers.ListField(child=serializers.DictField(), required=False)
    note = serializers.CharField(required=False, allow_blank=True)


class StockAlertView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(summary="Get shop's back-in-stock alerts", responses={200: serializers.ListField(child=serializers.IntegerField())})
    def get(self, request: Request):
        return ok_response(get_my_stock_alerts(actor=request.user))

    @extend_schema(summary="Toggle back-in-stock alert for an item", request=StockAlertInputSerializer, responses={200: serializers.ListField(child=serializers.IntegerField())})
    def post(self, request: Request):
        return ok_response(
            set_stock_alert(
                actor=request.user,
                item_id=int(request.data.get("item_id") or 0),
                on=bool(request.data.get("on")),
            )
        )


class StockWaitlistView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(summary="List items shops are waiting for", responses={200: serializers.ListField(child=serializers.DictField())})
    def get(self, request: Request):
        return ok_response(get_stock_waitlist(actor=request.user))


class ClearanceListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(summary="List clearance offers", responses={200: serializers.ListField(child=serializers.DictField())})
    def get(self, request: Request):
        return ok_response(list_clearances_for_actor(actor=request.user))

    @extend_schema(summary="Start a near-expiry batch clearance", request=ClearanceInputSerializer, responses={200: serializers.DictField()})
    def post(self, request: Request):
        return ok_response(start_clearance(actor=request.user, data=request.data))


class ClearanceCancelView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(summary="Cancel an active clearance", responses={200: serializers.DictField()})
    def post(self, request: Request, clearance_id: int):
        return ok_response(cancel_clearance(actor=request.user, clearance_id=clearance_id))


class PriceScheduleListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(summary="List scheduled price changes", responses={200: serializers.ListField(child=serializers.DictField())})
    def get(self, request: Request):
        return ok_response(list_scheduled_prices_for_actor(actor=request.user))

    @extend_schema(summary="Schedule a price change", request=ScheduledPriceInputSerializer, responses={200: serializers.DictField()})
    def post(self, request: Request):
        return ok_response(create_scheduled_price(actor=request.user, data=request.data))


class PriceScheduleCancelView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(summary="Cancel a scheduled price change", responses={200: serializers.DictField()})
    def post(self, request: Request, schedule_id: int):
        return ok_response(cancel_scheduled_price(actor=request.user, schedule_id=schedule_id))


class ItemPriceHistoryView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(summary="Get price history and scheduled changes for an item", responses={200: serializers.DictField()})
    def get(self, request: Request, item_id: int):
        return ok_response(get_item_price_history(actor=request.user, item_id=item_id))
