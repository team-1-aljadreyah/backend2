"""URL routes for catalog under /api/v1/."""
from django.urls import path

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
    path("categories/rename", CategoryRenameView.as_view(), name="categories-rename"),
]
