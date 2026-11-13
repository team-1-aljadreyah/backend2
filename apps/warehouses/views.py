"""HTTP views for warehouse CRUD, zone layout, and temporary keeper coverage."""
from __future__ import annotations

from rest_framework.views import APIView

from apps.accounts.models import Role
from apps.accounts.permissions import role_permission
from apps.common.responses import created_response, ok_response
from apps.warehouses.services import (
    create_warehouse,
    delete_warehouse,
    save_warehouse_layout,
    set_warehouse_cover,
    update_warehouse,
)


class WarehouseCreateView(APIView):
    permission_classes = [role_permission(Role.MANAGER)]

    def post(self, request):
        return created_response(create_warehouse(actor=request.user, data=request.data))


class WarehouseDetailView(APIView):
    permission_classes = [role_permission(Role.MANAGER)]

    def patch(self, request, warehouse_id: int):
        return ok_response(update_warehouse(actor=request.user, warehouse_id=warehouse_id, data=request.data))

    def delete(self, request, warehouse_id: int):
        delete_warehouse(actor=request.user, warehouse_id=warehouse_id)
        return ok_response({"ok": True})


class WarehouseLayoutView(APIView):
    permission_classes = [role_permission(Role.MANAGER, Role.WAREHOUSE)]

    def patch(self, request, warehouse_id: int):
        return ok_response(save_warehouse_layout(actor=request.user, warehouse_id=warehouse_id, data=request.data))


class WarehouseCoverView(APIView):
    permission_classes = [role_permission(Role.MANAGER)]

    def post(self, request, warehouse_id: int):
        data = request.data if isinstance(request.data, dict) else {}
        return ok_response(
            set_warehouse_cover(
                actor=request.user,
                warehouse_id=warehouse_id,
                user_id=data.get("user_id"),
                hours=data.get("hours"),
            )
        )
