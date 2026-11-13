"""URL routes for realtime HTTP endpoints under /api/v1/."""
from django.urls import path

from .views import RealtimeTicketView

urlpatterns = [
    path("realtime/ticket", RealtimeTicketView.as_view(), name="realtime-ticket"),
]
