from django.urls import path

from .views_and_tasks import (
    NotificationListView,
    NotificationMarkAllReadView,
    NotificationMarkReadView,
    PushSubscriptionCreateView,
    PushSubscriptionDeleteView,
)

urlpatterns = [
    path("notifications", NotificationListView.as_view(), name="notifications-list"),
    path("notifications/read-all", NotificationMarkAllReadView.as_view(), name="notifications-read-all"),
    path("notifications/<int:notification_id>/read", NotificationMarkReadView.as_view(), name="notification-read"),
    path("push-subscriptions", PushSubscriptionCreateView.as_view(), name="push-subscriptions"),
    path("push-subscriptions/delete", PushSubscriptionDeleteView.as_view(), name="push-subscriptions-delete"),
]
