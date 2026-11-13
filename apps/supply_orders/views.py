"""HTTP views for supply orders."""
from __future__ import annotations

from rest_framework.views import APIView

from apps.accounts.models import Role
from apps.accounts.permissions import role_permission
from apps.common.responses import created_response, ok_response
from apps.supply_orders.services import (
    cancel_supply_order,
    create_supply_order,
    get_supply_order_for_actor,
    list_supply_orders_for_actor,
)


class SupplyOrderListCreateView(APIView):
    def get_permissions(self):
        if self.request.method == "POST":
            return [role_permission(Role.MANAGER, Role.ACCOUNTANT)()]
        return [role_permission(Role.MANAGER, Role.ACCOUNTANT, Role.WAREHOUSE, Role.STAFF)()]

    def get(self, request):
        return ok_response(list_supply_orders_for_actor(actor=request.user))

    def post(self, request):
        return created_response(create_supply_order(actor=request.user, data=request.data))


class SupplyOrderDetailView(APIView):
    permission_classes = [role_permission(Role.MANAGER, Role.ACCOUNTANT, Role.WAREHOUSE, Role.STAFF)]

    def get(self, request, supply_order_id: int):
        return ok_response(get_supply_order_for_actor(actor=request.user, supply_order_id=supply_order_id))


class SupplyOrderCancelView(APIView):
    permission_classes = [role_permission(Role.MANAGER, Role.ACCOUNTANT)]

    def post(self, request, supply_order_id: int):
        reason = request.data.get("reason", "") if isinstance(request.data, dict) else ""
        return ok_response(cancel_supply_order(actor=request.user, supply_order_id=supply_order_id, reason=reason))
