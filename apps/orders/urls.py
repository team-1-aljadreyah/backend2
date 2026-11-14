"""URL routes for orders under /api/v1/."""
from django.urls import path

from .views import (
    OrderAcceptView,
    OrderApproveView,
    OrderCancelView,
    OrderDeliveryCarrierView,
    OrderDetailUpdateView,
    OrderDiscountView,
    OrderListCreateView,
    OrderPrepareView,
    OrderPrepPlanView,
    OrderRatingView,
    OrderShipmentsView,
)

urlpatterns = [
    path("orders", OrderListCreateView.as_view(), name="orders-list-create"),
    path("orders/<int:order_id>", OrderDetailUpdateView.as_view(), name="order-detail"),
    path("orders/<int:order_id>/accept", OrderAcceptView.as_view(), name="order-accept"),
    path("orders/<int:order_id>/approve", OrderApproveView.as_view(), name="order-approve"),
    path("orders/<int:order_id>/prep-plan", OrderPrepPlanView.as_view(), name="order-prep-plan"),
    path("orders/<int:order_id>/prepare", OrderPrepareView.as_view(), name="order-prepare"),
    path("orders/<int:order_id>/delivery/carrier", OrderDeliveryCarrierView.as_view(), name="order-delivery-carrier"),
    path("orders/<int:order_id>/shipments", OrderShipmentsView.as_view(), name="order-shipments"),
    path("orders/<int:order_id>/discount", OrderDiscountView.as_view(), name="order-discount"),
    path("orders/<int:order_id>/cancel", OrderCancelView.as_view(), name="order-cancel"),
    path("orders/<int:order_id>/rating", OrderRatingView.as_view(), name="order-rating"),
]
