"""Notification and push subscription serializers for drf-spectacular."""
from __future__ import annotations

from rest_framework import serializers


class PushKeysSerializer(serializers.Serializer):
    p256dh = serializers.CharField()
    auth = serializers.CharField()


class PushSubscriptionInputSerializer(serializers.Serializer):
    endpoint = serializers.URLField()
    keys = PushKeysSerializer()


class PushSubscriptionDeleteInputSerializer(serializers.Serializer):
    endpoint = serializers.URLField()


class NotificationSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    target_role = serializers.CharField(required=False)
    user_id = serializers.IntegerField(required=False, allow_null=True)
    kind = serializers.CharField()
    title_ar = serializers.CharField()
    title_en = serializers.CharField()
    body_ar = serializers.CharField(required=False)
    body_en = serializers.CharField(required=False)
    ref_type = serializers.CharField(required=False)
    ref_id = serializers.CharField(required=False)
    created_at = serializers.DateTimeField()
    read = serializers.BooleanField(default=False)
