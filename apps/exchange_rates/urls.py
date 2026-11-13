from django.urls import path

from .services import UsdRateListCreateView

urlpatterns = [
    path("usd-rates", UsdRateListCreateView.as_view(), name="usd-rates"),
]
