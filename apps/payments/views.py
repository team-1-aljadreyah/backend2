"""DRF views for Payments, Reversals, Customer Statements/Refunds/CreditNotes, and Qi Card."""
from __future__ import annotations

from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.request import Request
from rest_framework.views import APIView

from apps.common.idempotency import run_idempotent
from apps.common.responses import ok_response
from apps.payments.serializers import (
    AllocationPreviewSerializer,
    CreditNoteSerializer,
    CustomerStatementSerializer,
    OnlinePaymentSerializer,
    PaymentInputSerializer,
    PaymentReverseInputSerializer,
    PaymentSerializer,
    RefundInputSerializer,
    RefundSerializer,
    StartOnlinePaymentInputSerializer,
)
from apps.payments.services import (
    create_payment,
    create_refund,
    get_customer_statement,
    get_online_payment_for_actor,
    get_payment_for_actor,
    handle_qi_card_webhook,
    list_credit_notes_for_customer,
    list_online_payments_for_actor,
    list_payments_for_actor,
    list_refunds_for_customer,
    preview_allocation,
    reverse_payment,
    start_online_payment,
)


class PaymentListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="List customer payments",
        responses={200: PaymentSerializer(many=True)},
    )
    def get(self, request: Request):
        return ok_response(data=list_payments_for_actor(actor=request.user))

    @extend_schema(
        summary="Record new customer payment",
        request=PaymentInputSerializer,
        responses={200: PaymentSerializer},
    )
    def post(self, request: Request):
        def _exec():
            data = create_payment(actor=request.user, data=request.data)
            return 200, {"success": True, "data": data}

        return run_idempotent(request=request, scope="payments.create", fn=_exec)


class PaymentDetailView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="Get payment details",
        responses={200: PaymentSerializer},
    )
    def get(self, request: Request, payment_id: int):
        return ok_response(data=get_payment_for_actor(actor=request.user, payment_id=payment_id))


class PaymentReverseView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="Reverse a payment entry",
        request=PaymentReverseInputSerializer,
        responses={200: PaymentSerializer},
    )
    def post(self, request: Request, payment_id: int):
        return ok_response(
            data=reverse_payment(
                actor=request.user,
                payment_id=payment_id,
                reason=str(request.data.get("reason") or ""),
            )
        )


class CustomerAllocationPreviewView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="Preview FIFO allocation across customer unpaid invoices",
        parameters=[OpenApiParameter(name="amount", type=int, required=False, location=OpenApiParameter.QUERY)],
        responses={200: AllocationPreviewSerializer(many=True)},
    )
    def get(self, request: Request, customer_id: int):
        raw_amt = request.query_params.get("amount") or "0"
        try:
            amount = int(raw_amt)
        except ValueError:
            amount = 0
        return ok_response(data=preview_allocation(actor=request.user, customer_id=customer_id, amount=amount))


class CustomerStatementView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="Get customer statement with running balance",
        parameters=[
            OpenApiParameter(name="from", type=str, required=False, location=OpenApiParameter.QUERY),
            OpenApiParameter(name="to", type=str, required=False, location=OpenApiParameter.QUERY),
        ],
        responses={200: CustomerStatementSerializer},
    )
    def get(self, request: Request, customer_id: int):
        return ok_response(
            data=get_customer_statement(
                actor=request.user,
                customer_id=customer_id,
                date_from=request.query_params.get("from") or None,
                date_to=request.query_params.get("to") or None,
            )
        )


class CustomerCreditNoteListView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="List customer credit notes",
        responses={200: CreditNoteSerializer(many=True)},
    )
    def get(self, request: Request, customer_id: int):
        return ok_response(data=list_credit_notes_for_customer(actor=request.user, customer_id=customer_id))


class CustomerRefundListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="List customer refunds",
        responses={200: RefundSerializer(many=True)},
    )
    def get(self, request: Request, customer_id: int):
        return ok_response(data=list_refunds_for_customer(actor=request.user, customer_id=customer_id))

    @extend_schema(
        summary="Create customer refund",
        request=RefundInputSerializer,
        responses={200: RefundSerializer},
    )
    def post(self, request: Request, customer_id: int):
        return ok_response(data=create_refund(actor=request.user, customer_id=customer_id, data=request.data))


class OnlinePaymentListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="List online payments",
        responses={200: OnlinePaymentSerializer(many=True)},
    )
    def get(self, request: Request):
        return ok_response(data=list_online_payments_for_actor(actor=request.user))

    @extend_schema(
        summary="Start online payment (Qi Card or ZainCash)",
        request=StartOnlinePaymentInputSerializer,
        responses={200: OnlinePaymentSerializer},
    )
    def post(self, request: Request):
        notif_url = request.build_absolute_uri("/api/v1/webhooks/qi-card")

        def _exec():
            data = start_online_payment(
                actor=request.user,
                data=request.data,
                notification_url=notif_url,
            )
            return 200, {"success": True, "data": data}

        return run_idempotent(request=request, scope="payments.online.create", fn=_exec)


class OnlinePaymentDetailView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="Get online payment status (Qi Card or ZainCash)",
        parameters=[
            OpenApiParameter(name="token", type=str, required=False, location=OpenApiParameter.QUERY, description="ZainCash return JWT token"),
        ],
        responses={200: OnlinePaymentSerializer},
    )
    def get(self, request: Request, key: str):
        return ok_response(
            data=get_online_payment_for_actor(
                actor=request.user,
                key=key,
                return_token=request.query_params.get("token"),
            )
        )


class QiCardWebhookView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    @extend_schema(
        summary="Qi Card payment gateway webhook receiver",
        responses={200: dict},
    )
    def post(self, request: Request):
        return ok_response(data=handle_qi_card_webhook(raw_body=request.body, headers=request.META))
