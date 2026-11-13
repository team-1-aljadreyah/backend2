from django.urls import path

from .views import (
    CommissionClosedListView,
    CommissionCloseView,
    CommissionLinesView,
    CommissionListView,
)

urlpatterns = [
    path("commissions", CommissionListView.as_view(), name="commissions-list"),
    path("commissions/close", CommissionCloseView.as_view(), name="commissions-close"),
    path("commissions/closed", CommissionClosedListView.as_view(), name="commissions-closed"),
    path("commissions/lines", CommissionLinesView.as_view(), name="commissions-lines"),
]
