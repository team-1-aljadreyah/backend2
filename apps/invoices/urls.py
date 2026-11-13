"""URL routes for invoices, counter sales, and returns under /api/v1/."""
from django.urls import path

from .views import (
    CounterSaleView,
    InvoiceCreditNoteView,
    InvoiceDetailView,
    InvoiceListView,
    ReturnDetailView,
    ReturnListCreateView,
)

urlpatterns = [
    path("invoices", InvoiceListView.as_view(), name="invoices-list"),
    path("invoices/<int:invoice_id>", InvoiceDetailView.as_view(), name="invoice-detail"),
    path("invoices/<int:invoice_id>/credit", InvoiceCreditNoteView.as_view(), name="invoice-credit"),
    path("counter-sales", CounterSaleView.as_view(), name="counter-sales"),
    path("returns", ReturnListCreateView.as_view(), name="returns-list-create"),
    path("returns/<int:return_id>", ReturnDetailView.as_view(), name="return-detail"),
]
