"""DRF views for Cash Custody («العهدة»)."""
from __future__ import annotations

from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.views import APIView

from apps.common.responses import ok_response
from apps.payments.custody_services import (
    cancel_cash_handover,
    confirm_cash_handover,
    create_cash_handover,
    get_custody_log_for_actor,
    list_custody_for_actor,
)


class HandoverCreateSerializer(serializers.Serializer):
    rep_id = serializers.IntegerField(required=False)
    amount = serializers.IntegerField(min_value=1)
    note = serializers.CharField(required=False, allow_blank=True)


class HandoverConfirmSerializer(serializers.Serializer):
    received = serializers.IntegerField(min_value=0)


class CustodyListView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(summary="List rep cash custody balances", responses={200: serializers.ListField(child=serializers.DictField())})
    def get(self, request: Request):
        return ok_response(list_custody_for_actor(actor=request.user))


class CustodyDetailView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(summary="Get one rep's cash custody log", responses={200: serializers.DictField()})
    def get(self, request: Request, rep_id: int):
        return ok_response(get_custody_log_for_actor(actor=request.user, rep_id=rep_id))


class CustodyHandoverCreateView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(summary="Create a cash handover", request=HandoverCreateSerializer, responses={200: serializers.DictField()})
    def post(self, request: Request):
        return ok_response(create_cash_handover(actor=request.user, data=request.data))


class CustodyHandoverConfirmView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(summary="Confirm a pending cash handover", request=HandoverConfirmSerializer, responses={200: serializers.DictField()})
    def post(self, request: Request, handover_id: int):
        return ok_response(
            confirm_cash_handover(actor=request.user, handover_id=handover_id, received_raw=request.data.get("received"))
        )


class CustodyHandoverCancelView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(summary="Cancel a pending cash handover", responses={200: serializers.DictField()})
    def post(self, request: Request, handover_id: int):
        return ok_response(cancel_cash_handover(actor=request.user, handover_id=handover_id))
