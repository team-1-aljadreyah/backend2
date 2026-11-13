from django.urls import path

from .views import (
    DailySalesReportView,
    DashboardView,
    DebtsReportView,
    DeliveryRatingsReportView,
    NearExpiryReportView,
    RepTargetsView,
    SalesReportView,
    SlowMovingReportView,
)

urlpatterns = [
    path("dashboard", DashboardView.as_view(), name="dashboard"),
    path("reports/daily-sales", DailySalesReportView.as_view(), name="report-daily-sales"),
    path("reports/sales", SalesReportView.as_view(), name="report-sales"),
    path("reports/slow-moving", SlowMovingReportView.as_view(), name="report-slow-moving"),
    path("reports/near-expiry", NearExpiryReportView.as_view(), name="report-near-expiry"),
    path("reports/debts", DebtsReportView.as_view(), name="report-debts"),
    path("reports/delivery-ratings", DeliveryRatingsReportView.as_view(), name="report-delivery-ratings"),
    path("reps/targets", RepTargetsView.as_view(), name="reps-targets"),
]
