"""Reports serializers for OpenAPI schemas and typed responses."""
from __future__ import annotations

from rest_framework import serializers

from apps.orders.serializers import DeliveryRatingSerializer, DeliveryRepRefSerializer


class RepRatingSummarySerializer(serializers.Serializer):
    rep = DeliveryRepRefSerializer()
    count = serializers.IntegerField()
    average = serializers.FloatField()
    low = serializers.IntegerField()


class LatestRatingRowSerializer(serializers.Serializer):
    order_id = serializers.IntegerField()
    number = serializers.CharField()
    customer_id = serializers.IntegerField()
    customer = serializers.CharField()
    rating = DeliveryRatingSerializer()


class RatingsReportSerializer(serializers.Serializer):
    count = serializers.IntegerField()
    average = serializers.FloatField()
    spread = serializers.ListField(child=serializers.IntegerField())
    reps = RepRatingSummarySerializer(many=True)
    latest = LatestRatingRowSerializer(many=True)
