"""DRF views for Invoices, Credit Notes, Returns, and Counter Sales."""
from __future__ import annotations

from drf_spectacular.utils import extend_schema
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.views import APIView

from apps.accounts.models import Role
from apps.accounts.permissions import role_permission
from apps.common.idempotency import run_idempotent
from apps.common.responses import ok_response
from apps.invoices.serializers import (
    CounterSaleInputSerializer,
    InvoiceSerializer,
    ReturnInputSerializer,
    ReturnSerializer,
)
from apps.invoices.services import (
    create_credit_note,
    create_return,
    get_invoice_for_actor,
    get_return_for_actor,
    list_invoices_for_actor,
    list_returns_for_actor,
    record_counter_sale,
)


class InvoiceListView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="List invoices for current actor scope",
        responses={200: InvoiceSerializer(many=True)},
    )
    def get(self, request: Request):
        return ok_response(data=list_invoices_for_actor(actor=request.user))


class InvoiceDetailView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="Retrieve invoice detail",
        responses={200: InvoiceSerializer},
    )
    def get(self, request: Request, invoice_id: int):
        return ok_response(data=get_invoice_for_actor(actor=request.user, invoice_id=invoice_id))


class InvoiceCreditNoteView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="Issue credit note on paid invoice",
    )
    def post(self, request: Request, invoice_id: int):
        return ok_response(data=create_credit_note(actor=request.user, invoice_id=invoice_id, data=request.data))


class ReturnListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="List sales returns",
        responses={200: ReturnSerializer(many=True)},
    )
    def get(self, request: Request):
        return ok_response(data=list_returns_for_actor(actor=request.user))

    @extend_schema(
        summary="Record a sales return on invoice",
        request=ReturnInputSerializer,
        responses={200: ReturnSerializer},
    )
    def post(self, request: Request):
        def _exec():
            data = create_return(actor=request.user, data=request.data)
            return 200, {"success": True, "data": data}

        return run_idempotent(request=request, scope="returns.create", fn=_exec)


class ReturnDetailView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="Retrieve sales return detail",
        responses={200: ReturnSerializer},
    )
    def get(self, request: Request, return_id: int):
        return ok_response(data=get_return_for_actor(actor=request.user, return_id=return_id))


class CounterSaleView(APIView):
    """Direct counter sale executed by warehouse keeper: immediate stock out & paid invoice."""

    permission_classes = [role_permission(Role.WAREHOUSE)]

    @extend_schema(
        summary="Warehouse keeper counter sale",
        request=CounterSaleInputSerializer,
        responses={200: InvoiceSerializer},
    )
    def post(self, request: Request):
        def _exec():
            inv_data = record_counter_sale(actor=request.user, data=request.data)
            return 200, {"success": True, "data": inv_data}

        key = request.headers.get("Idempotency-Key") or (
            request.data.get("client_ref") if isinstance(request.data, dict) else None
        )
        return run_idempotent(request=request, scope="counter_sales.create", fn=_exec, client_ref=key)
