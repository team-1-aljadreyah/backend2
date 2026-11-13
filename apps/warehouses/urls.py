"""URL routes for warehouses under /api/v1/."""
from django.urls import path

from .views import WarehouseCoverView, WarehouseCreateView, WarehouseDetailView, WarehouseLayoutView

urlpatterns = [
    path("warehouses", WarehouseCreateView.as_view(), name="warehouses-create"),
    path("warehouses/<int:warehouse_id>", WarehouseDetailView.as_view(), name="warehouse-detail"),
    path("warehouses/<int:warehouse_id>/layout", WarehouseLayoutView.as_view(), name="warehouse-layout"),
    path("warehouses/<int:warehouse_id>/cover", WarehouseCoverView.as_view(), name="warehouse-cover"),
]
