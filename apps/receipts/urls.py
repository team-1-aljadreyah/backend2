"""URL routes for receipts under /api/v1/."""
from django.urls import path

from .views import ReceiptDetailView, ReceiptListCreateView

urlpatterns = [
    path("receipts", ReceiptListCreateView.as_view(), name="receipts-list-create"),
    path("receipts/<int:receipt_id>", ReceiptDetailView.as_view(), name="receipt-detail"),
]
