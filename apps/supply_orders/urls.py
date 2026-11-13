"""URL patterns for supply orders."""
from __future__ import annotations

from django.urls import path

from apps.supply_orders.views import (
    SupplyOrderCancelView,
    SupplyOrderDetailView,
    SupplyOrderListCreateView,
)

urlpatterns = [
    path("supply-orders", SupplyOrderListCreateView.as_view(), name="supply-order-list-create"),
    path("supply-orders/<int:supply_order_id>", SupplyOrderDetailView.as_view(), name="supply-order-detail"),
    path("supply-orders/<int:supply_order_id>/cancel", SupplyOrderCancelView.as_view(), name="supply-order-cancel"),
]
