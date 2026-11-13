"""URL routes for inventory under /api/v1/."""
from django.urls import path

from .views import (
    BatchHoldView,
    BatchListView,
    BatchReleaseView,
    BatchTraceView,
    DamagedListView,
    ShelfStockView,
)

urlpatterns = [
    path("batches", BatchListView.as_view(), name="batches-list"),
    path("stock", ShelfStockView.as_view(), name="shelf-stock"),
    path("batches/<int:batch_id>/hold", BatchHoldView.as_view(), name="batch-hold"),
    path("batches/<int:batch_id>/release", BatchReleaseView.as_view(), name="batch-release"),
    path("batches/<int:batch_id>/trace", BatchTraceView.as_view(), name="batch-trace"),
    path("damaged", DamagedListView.as_view(), name="damaged-list"),
]
