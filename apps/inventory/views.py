"""Inventory API views: batches, shelf stock, hold/release/trace, damaged."""
from __future__ import annotations

from rest_framework.views import APIView

from apps.accounts.models import Role
from apps.accounts.permissions import role_permission
from apps.common.responses import ok_response
from apps.inventory.services import (
    build_shelf_rows,
    hold_batch,
    list_batches_for_actor,
    list_damaged_for_actor,
    release_batch,
    trace_batch_recipients,
)


def _int_param(request, key: str) -> int | None:
    raw = request.query_params.get(key)
    if raw in (None, ""):
        return None
    try:
        return int(raw)
    except ValueError:
        return None


class BatchListView(APIView):
    permission_classes = [role_permission(Role.MANAGER, Role.ACCOUNTANT, Role.WAREHOUSE, Role.STAFF)]

    def get(self, request):
        wh_id = _int_param(request, "warehouse_id")
        return ok_response(list_batches_for_actor(actor=request.user, warehouse_id=wh_id))


class ShelfStockView(APIView):
    permission_classes = [role_permission(Role.MANAGER, Role.ACCOUNTANT, Role.WAREHOUSE, Role.STAFF)]

    def get(self, request):
        wh_id = _int_param(request, "warehouse_id")
        return ok_response(build_shelf_rows(actor=request.user, warehouse_id=wh_id))


class BatchHoldView(APIView):
    permission_classes = [role_permission(Role.MANAGER, Role.WAREHOUSE)]

    def post(self, request, batch_id: int):
        reason = request.data.get("reason", "") if isinstance(request.data, dict) else ""
        return ok_response(hold_batch(actor=request.user, batch_id=batch_id, reason=reason))


class BatchReleaseView(APIView):
    permission_classes = [role_permission(Role.MANAGER)]

    def post(self, request, batch_id: int):
        return ok_response(release_batch(actor=request.user, batch_id=batch_id))


class BatchTraceView(APIView):
    permission_classes = [role_permission(Role.MANAGER, Role.ACCOUNTANT, Role.WAREHOUSE, Role.STAFF)]

    def get(self, request, batch_id: int):
        return ok_response(trace_batch_recipients(actor=request.user, batch_id=batch_id))


class DamagedListView(APIView):
    permission_classes = [role_permission(Role.MANAGER, Role.ACCOUNTANT, Role.WAREHOUSE, Role.STAFF)]

    def get(self, request):
        wh_id = _int_param(request, "warehouse_id")
        return ok_response(list_damaged_for_actor(actor=request.user, warehouse_id=wh_id))
