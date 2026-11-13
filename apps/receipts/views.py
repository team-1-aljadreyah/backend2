"""HTTP views for receipts."""
from __future__ import annotations

from rest_framework.views import APIView

from apps.accounts.models import Role
from apps.accounts.permissions import role_permission
from apps.common.idempotency import run_idempotent
from apps.common.responses import ok_response
from apps.receipts.services import create_receipt, get_receipt_for_actor, list_receipts_for_actor


class ReceiptListCreateView(APIView):
    def get_permissions(self):
        if self.request.method == "POST":
            return [role_permission(Role.MANAGER, Role.ACCOUNTANT, Role.WAREHOUSE)()]
        return [role_permission(Role.MANAGER, Role.ACCOUNTANT, Role.WAREHOUSE, Role.STAFF)()]

    def get(self, request):
        return ok_response(list_receipts_for_actor(actor=request.user))

    def post(self, request):
        def _exec():
            data = create_receipt(actor=request.user, data=request.data)
            return 201, {"success": True, "data": data}

        return run_idempotent(request=request, scope="receipts.create", fn=_exec)


class ReceiptDetailView(APIView):
    permission_classes = [role_permission(Role.MANAGER, Role.ACCOUNTANT, Role.WAREHOUSE, Role.STAFF)]

    def get(self, request, receipt_id: int):
        return ok_response(get_receipt_for_actor(actor=request.user, receipt_id=receipt_id))
