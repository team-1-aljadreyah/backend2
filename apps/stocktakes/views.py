"""HTTP views for stocktakes."""
from __future__ import annotations

from rest_framework.views import APIView

from apps.accounts.models import Role
from apps.accounts.permissions import role_permission
from apps.common.responses import created_response, ok_response
from apps.stocktakes.services import (
    close_stocktake,
    create_stocktake,
    get_stocktake_for_actor,
    list_stocktakes_for_actor,
    update_stocktake,
)


class StocktakeListCreateView(APIView):
    def get_permissions(self):
        if self.request.method == "POST":
            return [role_permission(Role.MANAGER, Role.WAREHOUSE)()]
        return [role_permission(Role.MANAGER, Role.ACCOUNTANT, Role.WAREHOUSE, Role.STAFF)()]

    def get(self, request):
        return ok_response(list_stocktakes_for_actor(actor=request.user))

    def post(self, request):
        return created_response(create_stocktake(actor=request.user, data=request.data))


class StocktakeDetailUpdateView(APIView):
    def get_permissions(self):
        if self.request.method == "PATCH":
            return [role_permission(Role.MANAGER, Role.WAREHOUSE)()]
        return [role_permission(Role.MANAGER, Role.ACCOUNTANT, Role.WAREHOUSE, Role.STAFF)()]

    def get(self, request, stocktake_id: int):
        return ok_response(get_stocktake_for_actor(actor=request.user, stocktake_id=stocktake_id))

    def patch(self, request, stocktake_id: int):
        return ok_response(update_stocktake(actor=request.user, stocktake_id=stocktake_id, data=request.data))


class StocktakeCloseView(APIView):
    permission_classes = [role_permission(Role.MANAGER, Role.WAREHOUSE)]

    def post(self, request, stocktake_id: int):
        return ok_response(close_stocktake(actor=request.user, stocktake_id=stocktake_id))
