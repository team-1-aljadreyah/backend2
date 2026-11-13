"""WebSocket URL routing for Django Channels."""
from django.urls import re_path

from .consumers import RealtimeConsumer

websocket_urlpatterns = [
    re_path(r"^ws/?$", RealtimeConsumer.as_asgi()),
]
