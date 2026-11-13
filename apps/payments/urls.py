"""URL routes for payments, refunds, statements, credit notes, and Qi Card under /api/v1/."""
from django.urls import path

from .custody_views import (
    CustodyDetailView,
    CustodyHandoverCancelView,
    CustodyHandoverConfirmView,
    CustodyHandoverCreateView,
    CustodyListView,
)
from .views import (
    CustomerAllocationPreviewView,
    CustomerCreditNoteListView,
    CustomerRefundListCreateView,
    CustomerStatementView,
    OnlinePaymentDetailView,
    OnlinePaymentListCreateView,
    PaymentDetailView,
    PaymentListCreateView,
    PaymentReverseView,
    QiCardWebhookView,
)

urlpatterns = [
    path("custody", CustodyListView.as_view(), name="custody-list"),
    path("custody/handovers", CustodyHandoverCreateView.as_view(), name="custody-handover-create"),
    path("custody/handovers/<int:handover_id>/confirm", CustodyHandoverConfirmView.as_view(), name="custody-handover-confirm"),
    path("custody/handovers/<int:handover_id>/cancel", CustodyHandoverCancelView.as_view(), name="custody-handover-cancel"),
    path("custody/<int:rep_id>", CustodyDetailView.as_view(), name="custody-detail"),
    path("payments", PaymentListCreateView.as_view(), name="payments-list-create"),
    path("payments/online", OnlinePaymentListCreateView.as_view(), name="payments-online-list-create"),
    path("payments/online/<str:key>", OnlinePaymentDetailView.as_view(), name="payments-online-detail"),
    path("payments/<int:payment_id>", PaymentDetailView.as_view(), name="payment-detail"),
    path("payments/<int:payment_id>/reverse", PaymentReverseView.as_view(), name="payment-reverse"),
    path("customers/<int:customer_id>/allocation", CustomerAllocationPreviewView.as_view(), name="customer-allocation"),
    path("customers/<int:customer_id>/statement", CustomerStatementView.as_view(), name="customer-statement"),
    path("customers/<int:customer_id>/credit-notes", CustomerCreditNoteListView.as_view(), name="customer-credit-notes"),
    path("customers/<int:customer_id>/refunds", CustomerRefundListCreateView.as_view(), name="customer-refunds"),
    path("webhooks/qi-card", QiCardWebhookView.as_view(), name="qi-card-webhook"),
]
