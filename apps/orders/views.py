"""HTTP views for orders."""
from __future__ import annotations

from drf_spectacular.utils import extend_schema
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from apps.accounts.models import Role
from apps.accounts.permissions import role_permission
from apps.common.idempotency import run_idempotent
from apps.common.responses import ok_response
from apps.orders.serializers import (
    DeliveryRatingInputSerializer,
    OrderCancelInputSerializer,
    OrderDiscountInputSerializer,
    OrderInputSerializer,
    OrderSerializer,
    PrepPlanSerializer,
)
from apps.orders.services import (
    accept_order,
    apply_order_discount,
    approve_order,
    cancel_order,
    create_order,
    get_order_for_actor,
    get_order_prep_plan,
    list_orders_for_actor,
    plan_shipments,
    prepare_order,
    rate_order_delivery,
    set_order_carrier,
    update_order,
)


class OrderListCreateView(APIView):
    def get_permissions(self):
        if self.request.method == "POST":
            return [role_permission(Role.MANAGER, Role.ACCOUNTANT, Role.REP, Role.SHOP)()]
        return [IsAuthenticated()]

    @extend_schema(
        summary="List orders for current actor scope",
        responses={200: OrderSerializer(many=True)},
    )
    def get(self, request):
        return ok_response(list_orders_for_actor(actor=request.user))

    @extend_schema(
        summary="Create new customer order",
        request=OrderInputSerializer,
        responses={201: OrderSerializer},
    )
    def post(self, request):
        def _exec():
            data = create_order(actor=request.user, data=request.data)
            return 201, {"success": True, "data": data}

        return run_idempotent(request=request, scope="orders.create", fn=_exec)


class OrderDetailUpdateView(APIView):
    def get_permissions(self):
        if self.request.method == "PATCH":
            return [role_permission(Role.MANAGER, Role.ACCOUNTANT, Role.REP, Role.SHOP)()]
        return [IsAuthenticated()]

    @extend_schema(
        summary="Get order detail",
        responses={200: OrderSerializer},
    )
    def get(self, request, order_id: int):
        return ok_response(get_order_for_actor(actor=request.user, order_id=order_id))

    @extend_schema(
        summary="Update order lines or note",
        request=OrderInputSerializer,
        responses={200: OrderSerializer},
    )
    def patch(self, request, order_id: int):
        return ok_response(update_order(actor=request.user, order_id=order_id, data=request.data))


class OrderAcceptView(APIView):
    permission_classes = [role_permission(Role.MANAGER, Role.ACCOUNTANT)]

    @extend_schema(
        summary="Accept order and advance to preparing",
        responses={200: OrderSerializer},
    )
    def post(self, request, order_id: int):
        return ok_response(accept_order(actor=request.user, order_id=order_id))


class OrderApproveView(APIView):
    permission_classes = [role_permission(Role.MANAGER, Role.ACCOUNTANT)]

    @extend_schema(
        summary="Approve order debt or discount override",
        responses={200: OrderSerializer},
    )
    def post(self, request, order_id: int):
        return ok_response(approve_order(actor=request.user, order_id=order_id))


class OrderPrepPlanView(APIView):
    permission_classes = [role_permission(Role.MANAGER, Role.WAREHOUSE)]

    @extend_schema(
        summary="Get preparation picking plan and available FEFO batches",
        responses={200: PrepPlanSerializer},
    )
    def get(self, request, order_id: int):
        return ok_response(get_order_prep_plan(actor=request.user, order_id=order_id))


class OrderPrepareView(APIView):
    permission_classes = [role_permission(Role.MANAGER, Role.WAREHOUSE)]

    @extend_schema(
        summary="Complete order preparation and mark ready for delivery",
        responses={200: OrderSerializer},
    )
    def post(self, request, order_id: int):
        body = request.data if isinstance(request.data, dict) else {}
        warehouses = body.get("warehouses", {})
        carrier = body.get("carrier") if isinstance(body.get("carrier"), dict) else {}
        rep_id = body.get("rep_id", carrier.get("rep_id"))
        vehicle_id = body.get("vehicle_id", carrier.get("vehicle_id"))
        return ok_response(
            prepare_order(
                actor=request.user,
                order_id=order_id,
                warehouses_map=warehouses,
                rep_id=rep_id,
                vehicle_id=vehicle_id,
            )
        )


class OrderDeliveryCarrierView(APIView):
    permission_classes = [role_permission(Role.MANAGER, Role.WAREHOUSE, Role.STAFF)]

    @extend_schema(
        summary="Assign or update delivery rep and vehicle before shipment is loaded",
        responses={200: OrderSerializer},
    )
    def post(self, request, order_id: int):
        body = request.data if isinstance(request.data, dict) else {}
        return ok_response(set_order_carrier(actor=request.user, order_id=order_id, data=body))


class OrderDiscountView(APIView):
    permission_classes = [role_permission(Role.MANAGER, Role.ACCOUNTANT, Role.REP)]

    @extend_schema(
        summary="Apply line discount percent",
        request=OrderDiscountInputSerializer,
        responses={200: OrderSerializer},
    )
    def post(self, request, order_id: int):
        pct = request.data.get("percent", 0) if isinstance(request.data, dict) else 0
        return ok_response(apply_order_discount(actor=request.user, order_id=order_id, percent=pct))


class OrderCancelView(APIView):
    permission_classes = [role_permission(Role.MANAGER, Role.ACCOUNTANT, Role.REP, Role.SHOP)]

    @extend_schema(
        summary="Cancel an unfulfilled order",
        request=OrderCancelInputSerializer,
        responses={200: OrderSerializer},
    )
    def post(self, request, order_id: int):
        reason = request.data.get("reason", "") if isinstance(request.data, dict) else ""
        return ok_response(cancel_order(actor=request.user, order_id=order_id, reason=reason))


class OrderShipmentsView(APIView):
    permission_classes = [role_permission(Role.MANAGER, Role.WAREHOUSE, Role.STAFF)]

    @extend_schema(
        summary="Plan delivery loads and vehicle assignment",
        responses={200: dict},
    )
    def post(self, request, order_id: int):
        loads = request.data.get("loads", []) if isinstance(request.data, dict) else []
        return ok_response(plan_shipments(actor=request.user, order_id=order_id, loads=loads))


class OrderRatingView(APIView):
    permission_classes = [role_permission(Role.SHOP)]

    @extend_schema(
        summary="Submit 1-5 star delivery rating by shop owner within 7 days",
        request=DeliveryRatingInputSerializer,
        responses={200: OrderSerializer},
    )
    def post(self, request, order_id: int):
        return ok_response(rate_order_delivery(actor=request.user, order_id=order_id, data=request.data or {}))
