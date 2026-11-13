"""HTTP views for inter-warehouse transfers."""
from __future__ import annotations

from rest_framework.views import APIView

from apps.accounts.models import Role
from apps.accounts.permissions import role_permission
from apps.common.idempotency import run_idempotent
from apps.common.responses import ok_response
from apps.transfers.services import (
    create_transfer,
    list_transfers_for_actor,
    receive_transfer,
    review_transfer,
)


class TransferListCreateView(APIView):
    def get_permissions(self):
        if self.request.method == "POST":
            return [role_permission(Role.MANAGER, Role.WAREHOUSE)()]
        return [role_permission(Role.MANAGER, Role.ACCOUNTANT, Role.WAREHOUSE, Role.STAFF)()]

    def get(self, request):
        return ok_response(list_transfers_for_actor(actor=request.user))

    def post(self, request):
        def _exec():
            data = create_transfer(actor=request.user, data=request.data)
            return 201, {"success": True, "data": data}

        return run_idempotent(request=request, scope="transfers.create", fn=_exec)


class TransferReceiveView(APIView):
    permission_classes = [role_permission(Role.MANAGER, Role.WAREHOUSE)]

    def post(self, request, transfer_id: int):
        return ok_response(receive_transfer(actor=request.user, transfer_id=transfer_id, data=request.data))


class TransferReviewView(APIView):
    permission_classes = [role_permission(Role.MANAGER)]

    def post(self, request, transfer_id: int):
        return ok_response(review_transfer(actor=request.user, transfer_id=transfer_id))
