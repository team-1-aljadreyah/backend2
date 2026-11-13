"""DRF views for Deliveries (QR dispatch, PIN delivery, unlock, problem, return, private proof photo)."""
from __future__ import annotations

from drf_spectacular.utils import extend_schema
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.views import APIView

from apps.accounts.models import Role
from apps.accounts.permissions import role_permission
from apps.common.responses import ok_response
from apps.deliveries.serializers import (
    DeliverInputSerializer,
    ProblemInputSerializer,
    RepLocationInputSerializer,
    RepLocationSerializer,
    VehicleInputSerializer,
    VehicleSerializer,
)
from apps.deliveries.services import (
    deliver_order,
    dispatch_delivery,
    get_delivery_photo_url,
    list_rep_locations,
    load_shipment,
    report_delivery_problem,
    return_delivery_to_warehouse,
    save_vehicle,
    unlock_delivery,
    update_rep_location,
)
from apps.orders.serializers import OrderSerializer


class DeliveryDispatchView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="Dispatch delivery using scanned QR code",
        responses={200: OrderSerializer},
    )
    def post(self, request: Request):
        return ok_response(data=dispatch_delivery(actor=request.user, code=str(request.data.get("code") or "")))


class OrderDeliverView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="Verify PIN and confirm delivery (full or partial)",
        request=DeliverInputSerializer,
        responses={200: OrderSerializer},
    )
    def post(self, request: Request, order_id: int):
        return ok_response(data=deliver_order(actor=request.user, order_id=order_id, data=request.data))


class DeliveryUnlockView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="Manager unlock 5-attempt locked delivery PIN",
        responses={200: OrderSerializer},
    )
    def post(self, request: Request, order_id: int):
        return ok_response(data=unlock_delivery(actor=request.user, order_id=order_id))


class DeliveryProblemView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="Report delivery issue (customer closed, accident, etc.)",
        request=ProblemInputSerializer,
        responses={200: OrderSerializer},
    )
    def post(self, request: Request):
        return ok_response(data=report_delivery_problem(actor=request.user, data=request.data))


class DeliveryReturnToWarehouseView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="Return undelivered shipment to warehouse stock",
        responses={200: OrderSerializer},
    )
    def post(self, request: Request, order_id: int):
        return ok_response(data=return_delivery_to_warehouse(actor=request.user, order_id=order_id))


class DeliveryPhotoView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="Get signed access URL for private delivery proof photo",
        responses={200: dict},
    )
    def get(self, request: Request, order_id: int):
        return ok_response(data=get_delivery_photo_url(actor=request.user, order_id=order_id, request=request))


class RepLocationUpdateView(APIView):
    permission_classes = [role_permission(Role.REP)]

    @extend_schema(
        summary="Update delivery representative GPS telemetry",
        request=RepLocationInputSerializer,
        responses={200: dict},
    )
    def post(self, request: Request):
        body = request.data if isinstance(request.data, dict) else {}
        return ok_response(data=update_rep_location(actor=request.user, lat=body.get("lat"), lng=body.get("lng")))


class RepLocationListView(APIView):
    permission_classes = [
        role_permission(Role.MANAGER, Role.WAREHOUSE, Role.ACCOUNTANT, Role.STAFF, Role.SHOP, Role.REP)
    ]

    @extend_schema(
        summary="List active delivery reps latest GPS positions",
        responses={200: RepLocationSerializer(many=True)},
    )
    def get(self, request: Request):
        return ok_response(data=list_rep_locations(actor=request.user))


class VehicleCreateView(APIView):
    permission_classes = [role_permission(Role.MANAGER)]

    @extend_schema(
        summary="Register fleet delivery vehicle",
        request=VehicleInputSerializer,
        responses={200: VehicleSerializer},
    )
    def post(self, request: Request):
        body = request.data if isinstance(request.data, dict) else {}
        return ok_response(data=save_vehicle(actor=request.user, data=body))


class VehicleUpdateView(APIView):
    permission_classes = [role_permission(Role.MANAGER)]

    @extend_schema(
        summary="Update fleet delivery vehicle",
        request=VehicleInputSerializer,
        responses={200: VehicleSerializer},
    )
    def patch(self, request: Request, vehicle_id: int):
        body = request.data if isinstance(request.data, dict) else {}
        return ok_response(data=save_vehicle(actor=request.user, vehicle_id=vehicle_id, data=body))


class DeliveryLoadView(APIView):
    permission_classes = [role_permission(Role.MANAGER, Role.WAREHOUSE, Role.STAFF)]

    @extend_schema(
        summary="Load shipment onto vehicle",
        responses={200: dict},
    )
    def post(self, request: Request, order_id: int):
        return ok_response(data=load_shipment(actor=request.user, order_id=order_id))
