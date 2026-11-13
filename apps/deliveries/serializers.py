"""DRF Serializers for deliveries, GPS tracking, and vehicles."""
from __future__ import annotations

from rest_framework import serializers


class DeliveryPersonSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    name = serializers.CharField()


class DeliveryProblemSerializer(serializers.Serializer):
    kind = serializers.CharField()
    note = serializers.CharField(allow_blank=True, required=False)
    at = serializers.CharField()
    by = DeliveryPersonSerializer()


class DeliverySerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=["pending", "out_for_delivery", "delivered", "problem", "returned"])
    out_at = serializers.CharField(required=False, allow_null=True)
    out_by = DeliveryPersonSerializer(required=False, allow_null=True)
    out_phone = serializers.CharField(required=False, allow_blank=True)
    rep_id = serializers.IntegerField(required=False, allow_null=True)
    vehicle_id = serializers.IntegerField(required=False, allow_null=True)
    qr = serializers.CharField()
    pin = serializers.CharField(required=False, allow_null=True)
    pin_attempts = serializers.IntegerField(required=False)
    locked = serializers.BooleanField(required=False)
    delivered_at = serializers.CharField(required=False, allow_null=True)
    delivered_by = DeliveryPersonSerializer(required=False, allow_null=True)
    proof_url = serializers.CharField(required=False, allow_null=True)
    signed_proof_url = serializers.CharField(required=False, allow_null=True)
    problem = DeliveryProblemSerializer(required=False, allow_null=True)


class DispatchInputSerializer(serializers.Serializer):
    rep_id = serializers.IntegerField(required=False, allow_null=True)
    vehicle_id = serializers.IntegerField(required=False, allow_null=True)


class DeliverLineInputSerializer(serializers.Serializer):
    item_id = serializers.IntegerField()
    unit = serializers.ChoiceField(choices=["carton", "piece"])
    qty = serializers.IntegerField(min_value=1)


class DeliverInputSerializer(serializers.Serializer):
    pin = serializers.CharField(max_length=6)
    photo = serializers.CharField(required=False, allow_blank=True)
    lines = DeliverLineInputSerializer(many=True, required=False)
    rest = serializers.ChoiceField(choices=["keep", "cancel"], required=False, default="keep")


class ProblemInputSerializer(serializers.Serializer):
    order_ids = serializers.ListField(child=serializers.IntegerField(), required=False)
    reason = serializers.CharField()
    note = serializers.CharField(required=False, allow_blank=True, default="")


class DeliveryPhotoInputSerializer(serializers.Serializer):
    photo = serializers.CharField()


class RepLocationInputSerializer(serializers.Serializer):
    lat = serializers.FloatField()
    lng = serializers.FloatField()
    accuracy = serializers.FloatField(required=False, allow_null=True)


class RepLocationSerializer(serializers.Serializer):
    rep_id = serializers.IntegerField()
    lat = serializers.FloatField()
    lng = serializers.FloatField()
    accuracy = serializers.FloatField(required=False, allow_null=True)
    updated_at = serializers.CharField()


class VehicleSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    name = serializers.CharField()
    plate = serializers.CharField(allow_blank=True)
    capacity_kg = serializers.IntegerField()
    driver_id = serializers.IntegerField(allow_null=True)
    active = serializers.BooleanField()


class VehicleInputSerializer(serializers.Serializer):
    name = serializers.CharField()
    plate = serializers.CharField(required=False, allow_blank=True, default="")
    capacity_kg = serializers.IntegerField(min_value=1)
    driver_id = serializers.IntegerField(required=False, allow_null=True)
    active = serializers.BooleanField(required=False, default=True)
