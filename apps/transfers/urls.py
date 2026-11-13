"""URL routes for transfers under /api/v1/."""
from django.urls import path

from .views import TransferListCreateView, TransferReceiveView, TransferReviewView

urlpatterns = [
    path("transfers", TransferListCreateView.as_view(), name="transfers-list-create"),
    path("transfers/<int:transfer_id>/receive", TransferReceiveView.as_view(), name="transfer-receive"),
    path("transfers/<int:transfer_id>/review", TransferReviewView.as_view(), name="transfer-review"),
]
