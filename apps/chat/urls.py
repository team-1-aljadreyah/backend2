from django.urls import path

from .views import ChatCustomerThreadView, ChatMarkReadView, ChatMeView, ChatThreadsView

urlpatterns = [
    path("chat/me", ChatMeView.as_view(), name="chat-me"),
    path("chat", ChatThreadsView.as_view(), name="chat-threads"),
    path("chat/<int:customer_id>", ChatCustomerThreadView.as_view(), name="chat-customer"),
    path("chat/<int:customer_id>/read", ChatMarkReadView.as_view(), name="chat-read"),
]
