"""URL routes for stocktakes under /api/v1/."""
from django.urls import path

from .views import StocktakeCloseView, StocktakeDetailUpdateView, StocktakeListCreateView

urlpatterns = [
    path("stocktakes", StocktakeListCreateView.as_view(), name="stocktakes-list-create"),
    path("stocktakes/<int:stocktake_id>", StocktakeDetailUpdateView.as_view(), name="stocktake-detail"),
    path("stocktakes/<int:stocktake_id>/close", StocktakeCloseView.as_view(), name="stocktake-close"),
]
