"""URL routes for customers, /me/shops, and /shop-lists under /api/v1/."""
from django.urls import path

from .views import (
    CustomerDetailUpdateView,
    CustomerListCreateView,
    CustomerRejectView,
    CustomerSpecialPricesView,
    MyShopsListCreateView,
    MyShopSwitchView,
    ShopListDetailView,
    ShopListsView,
)

urlpatterns = [
    path("customers", CustomerListCreateView.as_view(), name="customers-list-create"),
    path("customers/<int:customer_id>", CustomerDetailUpdateView.as_view(), name="customer-detail"),
    path("customers/<int:customer_id>/reject", CustomerRejectView.as_view(), name="customer-reject"),
    path("customers/<int:customer_id>/prices", CustomerSpecialPricesView.as_view(), name="customer-special-prices"),
    path("me/shops", MyShopsListCreateView.as_view(), name="me-shops"),
    path("me/shop", MyShopSwitchView.as_view(), name="me-shop-switch"),
    path("shop-lists", ShopListsView.as_view(), name="shop-lists"),
    path("shop-lists/<int:list_id>", ShopListDetailView.as_view(), name="shop-list-detail"),
]
