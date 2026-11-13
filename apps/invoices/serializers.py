"""Invoice, Return, and CounterSale serializers for API responses and OpenAPI docs."""
from __future__ import annotations

from rest_framework import serializers


class CounterSaleLineInputSerializer(serializers.Serializer):
    item_id = serializers.IntegerField()
    unit = serializers.ChoiceField(choices=["carton", "piece"])
    qty = serializers.IntegerField(min_value=1)


class CounterSaleInputSerializer(serializers.Serializer):
    client_ref = serializers.CharField()
    warehouse_id = serializers.IntegerField()
    tier_id = serializers.IntegerField()
    lines = CounterSaleLineInputSerializer(many=True)
    buyer_name = serializers.CharField(required=False, allow_blank=True, max_length=80)
    buyer_phone = serializers.CharField(required=False, allow_blank=True)
    method = serializers.ChoiceField(choices=["cash", "transfer"])
    received = serializers.IntegerField(required=False, min_value=0)
    note = serializers.CharField(required=False, allow_blank=True)


class InvoiceBatchAllocSerializer(serializers.Serializer):
    batch_id = serializers.IntegerField()
    expiry_date = serializers.CharField()
    qty = serializers.IntegerField()
    purchase_price = serializers.IntegerField(required=False)


class InvoiceLineSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    item_id = serializers.IntegerField()
    warehouse_id = serializers.IntegerField()
    unit = serializers.ChoiceField(choices=["carton", "piece"])
    qty = serializers.IntegerField()
    pieces = serializers.IntegerField()
    returned_pieces = serializers.IntegerField(default=0)
    unit_price = serializers.IntegerField()
    offer_off = serializers.IntegerField(required=False)
    total = serializers.IntegerField()
    batches = InvoiceBatchAllocSerializer(many=True, required=False)


class CounterInfoSerializer(serializers.Serializer):
    tier_id = serializers.IntegerField()
    method = serializers.ChoiceField(choices=["cash", "transfer"])
    buyer_name = serializers.CharField(required=False)
    buyer_phone = serializers.CharField(required=False)
    received = serializers.IntegerField(required=False)


class InvoiceSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    number = serializers.CharField()
    date = serializers.CharField()
    order_id = serializers.IntegerField()
    customer_id = serializers.IntegerField()
    rep_id = serializers.IntegerField()
    delivery_rep_id = serializers.IntegerField()
    warehouse_id = serializers.IntegerField()
    subtotal = serializers.IntegerField()
    discount_percent = serializers.FloatField()
    discount = serializers.IntegerField()
    rounding = serializers.IntegerField()
    total = serializers.IntegerField()
    usd_rate = serializers.FloatField()
    returned = serializers.IntegerField()
    credited = serializers.IntegerField()
    status = serializers.ChoiceField(choices=["paid", "partial", "open"])
    paid = serializers.IntegerField(required=False)
    remaining = serializers.IntegerField(required=False)
    cost = serializers.IntegerField(required=False)
    profit = serializers.IntegerField(required=False)
    counter = CounterInfoSerializer(required=False)
    lines = InvoiceLineSerializer(many=True)


class PersonRefSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    name = serializers.CharField()


class ReturnLineSerializer(serializers.Serializer):
    item_id = serializers.IntegerField()
    unit = serializers.ChoiceField(choices=["carton", "piece"])
    qty = serializers.IntegerField()
    pieces = serializers.IntegerField()
    amount = serializers.IntegerField()


class ReturnSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    number = serializers.CharField()
    client_ref = serializers.CharField()
    invoice_id = serializers.IntegerField()
    customer_id = serializers.IntegerField()
    rep_id = serializers.IntegerField()
    warehouse_id = serializers.IntegerField(required=False)
    lines = ReturnLineSerializer(many=True)
    amount = serializers.IntegerField()
    note = serializers.CharField(required=False, allow_blank=True)
    created_at = serializers.CharField()
    created_by = PersonRefSerializer()


class ReturnInputLineSerializer(serializers.Serializer):
    item_id = serializers.IntegerField()
    unit = serializers.ChoiceField(choices=["carton", "piece"])
    qty = serializers.IntegerField(min_value=1)


class ReturnInputSerializer(serializers.Serializer):
    invoice_id = serializers.IntegerField()
    lines = ReturnInputLineSerializer(many=True)
    client_ref = serializers.CharField(required=False, allow_blank=True)
    note = serializers.CharField(required=False, allow_blank=True, default="")
