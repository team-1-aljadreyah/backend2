"""HTTP views for expenses under /api/v1/."""
from __future__ import annotations

from rest_framework.views import APIView

from apps.accounts.models import Role
from apps.accounts.permissions import role_permission
from apps.common.responses import created_response, ok_response
from apps.expenses.services import (
    cancel_expense,
    create_expense,
    decide_expense,
    get_expense,
    list_expenses,
)


class ExpenseListCreateView(APIView):
    permission_classes = [role_permission(Role.MANAGER, Role.ACCOUNTANT)]

    def get(self, request):
        wh_raw = request.query_params.get("warehouse_id")
        wh_id = int(wh_raw) if wh_raw and str(wh_raw).isdigit() else None
        return ok_response(
            list_expenses(
                status_filter=request.query_params.get("status") or None,
                category=request.query_params.get("category") or None,
                warehouse_id=wh_id,
                date_from=request.query_params.get("from") or None,
                date_to=request.query_params.get("to") or None,
                request=request,
            )
        )

    def post(self, request):
        return created_response(create_expense(actor=request.user, data=request.data, request=request))


class ExpenseDetailView(APIView):
    permission_classes = [role_permission(Role.MANAGER, Role.ACCOUNTANT)]

    def get(self, request, expense_id: int):
        return ok_response(get_expense(expense_id=expense_id, request=request))


class ExpenseDecideView(APIView):
    permission_classes = [role_permission(Role.MANAGER)]

    def post(self, request, expense_id: int):
        data = request.data if isinstance(request.data, dict) else {}
        return ok_response(
            decide_expense(
                actor=request.user,
                expense_id=expense_id,
                ok=bool(data.get("ok")),
                reason=str(data.get("reason") or ""),
                request=request,
            )
        )


class ExpenseCancelView(APIView):
    permission_classes = [role_permission(Role.MANAGER, Role.ACCOUNTANT)]

    def post(self, request, expense_id: int):
        data = request.data if isinstance(request.data, dict) else {}
        return ok_response(
            cancel_expense(
                actor=request.user,
                expense_id=expense_id,
                reason=str(data.get("reason") or ""),
                request=request,
            )
        )
