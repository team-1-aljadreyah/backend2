"""URL routes for catalog under /api/v1/."""
from django.urls import path

from .features_views import (
    ClearanceCancelView,
    ClearanceListCreateView,
    ItemPriceHistoryView,
    PriceScheduleCancelView,
    PriceScheduleListCreateView,
    StockAlertView,
    StockWaitlistView,
)
from .views import (
    CatalogListView,
    CategoryRenameView,
    ItemBatchesView,
    ItemDetailView,
    ItemImageUploadView,
    ItemListCreateView,
    ItemQuickUpdateView,
    ItemVideoView,
)

urlpatterns = [
    path("catalog", CatalogListView.as_view(), name="catalog-list"),
    path("items", ItemListCreateView.as_view(), name="items-list-create"),
    path("items/<int:item_id>", ItemDetailView.as_view(), name="item-detail"),
    path("items/<int:item_id>/quick", ItemQuickUpdateView.as_view(), name="item-quick-update"),
    path("items/<int:item_id>/image", ItemImageUploadView.as_view(), name="item-image"),
    path("items/<int:item_id>/video", ItemVideoView.as_view(), name="item-video"),
    path("items/<int:item_id>/batches", ItemBatchesView.as_view(), name="item-batches"),
    path("items/<int:item_id>/price-history", ItemPriceHistoryView.as_view(), name="item-price-history"),
    path("stock-alerts", StockAlertView.as_view(), name="stock-alerts"),
    path("stock-alerts/waitlist", StockWaitlistView.as_view(), name="stock-alerts-waitlist"),
    path("clearances", ClearanceListCreateView.as_view(), name="clearances-list-create"),
    path("clearances/<int:clearance_id>/cancel", ClearanceCancelView.as_view(), name="clearance-cancel"),
    path("price-schedule", PriceScheduleListCreateView.as_view(), name="price-schedule-list-create"),
    path("price-schedule/<int:schedule_id>/cancel", PriceScheduleCancelView.as_view(), name="price-schedule-cancel"),
    path("categories/rename", CategoryRenameView.as_view(), name="categories-rename"),
]
