"""Catalog DRF serializers for API serialization and drf-spectacular schemas."""
from __future__ import annotations

from rest_framework import serializers


class QtyOfferSerializer(serializers.Serializer):
    unit = serializers.ChoiceField(choices=["carton", "piece"])
    min = serializers.IntegerField(min_value=2)
    off = serializers.IntegerField(min_value=1)
    until = serializers.DateField(required=False, allow_null=True)


class ItemClearanceSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    percent = serializers.IntegerField()
    warehouse_id = serializers.IntegerField()
    until = serializers.CharField()
    left = serializers.IntegerField()


class ItemPriceSerializer(serializers.Serializer):
    tier_id = serializers.IntegerField()
    carton_price = serializers.IntegerField(min_value=0)
    piece_price = serializers.IntegerField(min_value=0)


class StockByWarehouseSerializer(serializers.Serializer):
    warehouse_id = serializers.IntegerField()
    qty = serializers.IntegerField()


class ItemStockSerializer(serializers.Serializer):
    total = serializers.IntegerField()
    by_warehouse = StockByWarehouseSerializer(many=True)


class CatalogItemSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    name = serializers.CharField()
    name_en = serializers.CharField()
    category = serializers.CharField()
    brand = serializers.CharField(required=False, allow_blank=True)
    image = serializers.CharField(required=False, allow_blank=True)
    video = serializers.CharField(required=False, allow_blank=True)
    pieces_per_carton = serializers.IntegerField()
    pack_name = serializers.CharField(required=False, allow_blank=True)
    piece_name = serializers.CharField(required=False, allow_blank=True)
    sells = serializers.ChoiceField(choices=["both", "pack", "piece"], required=False)
    in_stock = serializers.BooleanField()


class ItemSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    name = serializers.CharField()
    name_en = serializers.CharField()
    category = serializers.CharField()
    barcode = serializers.CharField(required=False, allow_blank=True)
    brand = serializers.CharField(required=False, allow_blank=True)
    offer = QtyOfferSerializer(required=False, allow_null=True)
    video = serializers.CharField(required=False, allow_blank=True)
    pieces_per_carton = serializers.IntegerField()
    min_stock = serializers.IntegerField()
    commission_percent = serializers.FloatField()
    prices = ItemPriceSerializer(many=True)
    active = serializers.BooleanField()
    deleted = serializers.BooleanField(required=False)
    clearance = ItemClearanceSerializer(required=False, allow_null=True)
    stock = ItemStockSerializer()
    nearest_expiry = serializers.CharField(required=False, allow_null=True)
    avg_cost = serializers.IntegerField(required=False)
    image = serializers.CharField(required=False, allow_blank=True)
    weight_kg = serializers.FloatField(required=False, allow_null=True)
    pack_name = serializers.CharField(required=False, allow_blank=True)
    piece_name = serializers.CharField(required=False, allow_blank=True)
    sells = serializers.ChoiceField(choices=["both", "pack", "piece"], required=False)
    custom = serializers.DictField(required=False)


class ItemInputSerializer(serializers.Serializer):
    name = serializers.CharField()
    name_en = serializers.CharField()
    category = serializers.CharField()
    barcode = serializers.CharField(required=False, allow_blank=True, default="")
    brand = serializers.CharField(required=False, allow_blank=True, max_length=40)
    offer = QtyOfferSerializer(required=False, allow_null=True)
    pack_name = serializers.CharField(required=False, allow_blank=True, max_length=20)
    piece_name = serializers.CharField(required=False, allow_blank=True, max_length=20)
    sells = serializers.ChoiceField(choices=["both", "pack", "piece"], required=False, default="both")
    pieces_per_carton = serializers.IntegerField(min_value=1)
    min_stock = serializers.IntegerField(min_value=0, default=0)
    commission_percent = serializers.FloatField(min_value=0, max_value=100, default=0)
    prices = ItemPriceSerializer(many=True)
    active = serializers.BooleanField(default=True)
    weight_kg = serializers.FloatField(required=False, allow_null=True, min_value=0.01, max_value=100.0)
    custom = serializers.DictField(required=False)


class ItemQuickUpdateSerializer(serializers.Serializer):
    name = serializers.CharField(required=False)
    name_en = serializers.CharField(required=False)
    category = serializers.CharField(required=False)
    barcode = serializers.CharField(required=False, allow_blank=True)
    brand = serializers.CharField(required=False, allow_blank=True, max_length=40)
    pack_name = serializers.CharField(required=False, allow_blank=True, max_length=20)
    piece_name = serializers.CharField(required=False, allow_blank=True, max_length=20)
    sells = serializers.ChoiceField(choices=["both", "pack", "piece"], required=False)
    pieces_per_carton = serializers.IntegerField(required=False, min_value=1)
    min_stock = serializers.IntegerField(required=False, min_value=0)
    active = serializers.BooleanField(required=False)
    weight_kg = serializers.FloatField(required=False, allow_null=True)
    prices = ItemPriceSerializer(many=True, required=False)


class ItemVideoInputSerializer(serializers.Serializer):
    video = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    file = serializers.FileField(required=False)


class CategoryRenameInputSerializer(serializers.Serializer):
    old = serializers.CharField(required=False)
    to = serializers.CharField(required=False)


class ItemDeleteResponseSerializer(serializers.Serializer):
    result = serializers.ChoiceField(choices=["removed", "hidden"])
