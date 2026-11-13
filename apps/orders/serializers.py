"""Order & Delivery Rating serializers for API schemas and drf-spectacular."""
from __future__ import annotations

from rest_framework import serializers

from apps.deliveries.serializers import DeliverySerializer


class DeliveryRatingInputSerializer(serializers.Serializer):
    stars = serializers.IntegerField(min_value=1, max_value=5)
    note = serializers.CharField(required=False, allow_blank=True, max_length=300)


class DeliveryRepRefSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    name = serializers.CharField()


class DeliveryRatingSerializer(serializers.Serializer):
    stars = serializers.IntegerField()
    note = serializers.CharField(required=False, allow_null=True)
    at = serializers.CharField()
    rep = DeliveryRepRefSerializer(required=False)


class OrderLineSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    item_id = serializers.IntegerField()
    warehouse_id = serializers.IntegerField()
    unit = serializers.ChoiceField(choices=["carton", "piece"])
    qty = serializers.IntegerField()
    pieces = serializers.IntegerField()
    unit_price = serializers.IntegerField()
    offer_off = serializers.IntegerField(required=False)
    total = serializers.IntegerField()


class OrderSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    number = serializers.CharField()
    client_ref = serializers.CharField()
    customer_id = serializers.IntegerField()
    rep_id = serializers.IntegerField()
    warehouse_id = serializers.IntegerField()
    source = serializers.ChoiceField(choices=["shop", "rep", "office", "counter"])
    status = serializers.CharField()
    lines = OrderLineSerializer(many=True)
    delivery = DeliverySerializer(required=False, allow_null=True)
    total = serializers.IntegerField()
    subtotal = serializers.IntegerField(required=False)
    rounding = serializers.IntegerField(required=False)
    rating = DeliveryRatingSerializer(required=False)
    counter = serializers.DictField(required=False)
    custom = serializers.DictField(required=False)


class OrderLineInputSerializer(serializers.Serializer):
    item_id = serializers.IntegerField()
    unit = serializers.ChoiceField(choices=["carton", "piece"])
    qty = serializers.IntegerField(min_value=1)
    warehouse_id = serializers.IntegerField(required=False)


class OrderInputSerializer(serializers.Serializer):
    client_ref = serializers.CharField(required=False)
    customer_id = serializers.IntegerField()
    warehouse_id = serializers.IntegerField(required=False)
    recorded_at = serializers.CharField(required=False)
    offline = serializers.BooleanField(required=False, default=False)
    notes = serializers.CharField(required=False, allow_blank=True, default="")
    lines = OrderLineInputSerializer(many=True)


class OrderDiscountInputSerializer(serializers.Serializer):
    percent = serializers.FloatField(min_value=0, max_value=30)


class OrderCancelInputSerializer(serializers.Serializer):
    reason = serializers.CharField(required=False, allow_blank=True, default="")


class PrepBatchOptionSerializer(serializers.Serializer):
    batch_id = serializers.IntegerField()
    warehouse_id = serializers.IntegerField()
    warehouse_name = serializers.CharField()
    qty_available = serializers.IntegerField()
    expiry_date = serializers.CharField()


class PrepLineSerializer(serializers.Serializer):
    item_id = serializers.IntegerField()
    item_name = serializers.CharField()
    unit = serializers.CharField()
    qty_needed = serializers.IntegerField()
    batches = PrepBatchOptionSerializer(many=True)


class PrepPlanSerializer(serializers.Serializer):
    order_id = serializers.IntegerField()
    lines = PrepLineSerializer(many=True)
