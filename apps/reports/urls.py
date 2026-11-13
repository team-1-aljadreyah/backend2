from django.urls import path

from .extra_views import (
    DaySummaryReportView,
    LateShopsView,
    ReorderDueView,
)
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
    path("reports/day-summary", DaySummaryReportView.as_view(), name="report-day-summary"),
    path("reorder/due", ReorderDueView.as_view(), name="reorder-due"),
    path("reorder/late-shops", LateShopsView.as_view(), name="reorder-late-shops"),
    path("reps/targets", RepTargetsView.as_view(), name="reps-targets"),
]
