"""Customer & ShopList serializers for API serialization and drf-spectacular schemas."""
from __future__ import annotations

from rest_framework import serializers


class ShopListLineSerializer(serializers.Serializer):
    item_id = serializers.IntegerField()
    unit = serializers.ChoiceField(choices=["carton", "piece"])
    qty = serializers.IntegerField(min_value=1)


class ShopListSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    customer_id = serializers.IntegerField()
    name = serializers.CharField(max_length=40)
    lines = ShopListLineSerializer(many=True)
    updated_at = serializers.DateTimeField()


class ShopListInputSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=40)
    lines = ShopListLineSerializer(many=True)


class CustomerAddressSerializer(serializers.Serializer):
    city = serializers.CharField(required=False, allow_blank=True)
    area = serializers.CharField(required=False, allow_blank=True)
    street = serializers.CharField(required=False, allow_blank=True)
    landmark = serializers.CharField(required=False, allow_blank=True)
    details = serializers.CharField(required=False, allow_blank=True)
    lat = serializers.FloatField(required=False, allow_null=True)
    lng = serializers.FloatField(required=False, allow_null=True)


class CustomerSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    name = serializers.CharField()
    owner_name = serializers.CharField()
    phone = serializers.CharField()
    city = serializers.CharField()
    area = serializers.CharField()
    street = serializers.CharField()
    landmark = serializers.CharField()
    address = serializers.CharField()
    address_info = CustomerAddressSerializer(required=False, allow_null=True)
    lat = serializers.FloatField(required=False, allow_null=True)
    lng = serializers.FloatField(required=False, allow_null=True)
    tier_id = serializers.IntegerField()
    rep_id = serializers.IntegerField(required=False, allow_null=True)
    debt_limit = serializers.IntegerField()
    active = serializers.BooleanField()
    balance = serializers.IntegerField(required=False)
    status = serializers.CharField()
    custom = serializers.DictField(required=False)


class CustomerInputSerializer(serializers.Serializer):
    name = serializers.CharField()
    owner_name = serializers.CharField(required=False, allow_blank=True)
    phone = serializers.CharField()
    area = serializers.CharField()
    address = serializers.CharField(required=False, allow_blank=True)
    address_info = CustomerAddressSerializer(required=False, allow_null=True)
    tier_id = serializers.IntegerField()
    rep_id = serializers.IntegerField(required=False, allow_null=True)
    debt_limit = serializers.IntegerField(default=0, min_value=0)
    active = serializers.BooleanField(default=True)
    custom = serializers.DictField(required=False)


class MyShopSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    name = serializers.CharField()
    owner_name = serializers.CharField()
    phone = serializers.CharField()
    area = serializers.CharField()
    address = serializers.CharField()
    address_info = CustomerAddressSerializer(required=False, allow_null=True)
    tier_id = serializers.IntegerField()
    active = serializers.BooleanField()
    status = serializers.CharField()
    is_active_shop = serializers.BooleanField()
    is_primary = serializers.BooleanField()
    balance = serializers.IntegerField(required=False)
    debt_limit = serializers.IntegerField(required=False)


class MyShopSwitchInputSerializer(serializers.Serializer):
    customer_id = serializers.IntegerField()
