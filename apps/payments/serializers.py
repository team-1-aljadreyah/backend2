"""DRF Serializers for Payments, Allocations, Refunds, Credit Notes, and Online Payments."""
from __future__ import annotations

from rest_framework import serializers

from apps.customers.serializers import CustomerSerializer


class PersonRoleSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    name = serializers.CharField()
    role = serializers.CharField()


class AllocationSerializer(serializers.Serializer):
    invoice_id = serializers.IntegerField()
    number = serializers.CharField()
    amount = serializers.IntegerField()


class GatewayInfoSerializer(serializers.Serializer):
    provider = serializers.ChoiceField(choices=["qi", "zaincash"])
    payment_id = serializers.CharField()
    card = serializers.CharField()
    status = serializers.CharField(required=False)


class PaymentSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    number = serializers.CharField()
    client_ref = serializers.CharField(required=False, allow_null=True)
    customer_id = serializers.IntegerField()
    amount = serializers.IntegerField()
    unallocated_amount = serializers.IntegerField()
    method = serializers.ChoiceField(choices=["cash", "transfer", "online"])
    date = serializers.CharField()
    received_by = PersonRoleSerializer()
    allocations = AllocationSerializer(many=True)
    reversed = serializers.BooleanField()
    reversal_of = serializers.IntegerField(required=False, allow_null=True)
    reversed_by_id = serializers.IntegerField(required=False, allow_null=True)
    note = serializers.CharField(required=False, allow_blank=True)
    reason = serializers.CharField(required=False, allow_blank=True)
    gateway = GatewayInfoSerializer(required=False, allow_null=True)


class PaymentInputSerializer(serializers.Serializer):
    customer_id = serializers.IntegerField()
    amount = serializers.IntegerField(min_value=1)
    method = serializers.ChoiceField(choices=["cash", "transfer", "online"], default="cash")
    note = serializers.CharField(required=False, allow_blank=True, default="")
    client_ref = serializers.CharField(required=False, allow_blank=True, allow_null=True)


class PaymentReverseInputSerializer(serializers.Serializer):
    reason = serializers.CharField(required=False, allow_blank=True, default="")


class RefundSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    number = serializers.CharField()
    customer_id = serializers.IntegerField()
    payment_id = serializers.IntegerField(required=False, allow_null=True)
    amount = serializers.IntegerField()
    method = serializers.ChoiceField(choices=["cash", "transfer", "online"])
    reason = serializers.CharField()
    by = PersonRoleSerializer()
    at = serializers.CharField()
    created_by = PersonRoleSerializer()
    created_at = serializers.CharField()


class RefundInputSerializer(serializers.Serializer):
    amount = serializers.IntegerField(min_value=1)
    method = serializers.ChoiceField(choices=["cash", "transfer", "online"], default="cash")
    payment_id = serializers.IntegerField(required=False, allow_null=True)
    reason = serializers.CharField()


class CreditNoteSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    number = serializers.CharField()
    customer_id = serializers.IntegerField()
    invoice_id = serializers.IntegerField()
    return_id = serializers.IntegerField(required=False, allow_null=True)
    amount = serializers.IntegerField()
    reason = serializers.CharField()
    created_at = serializers.CharField()
    created_by = PersonRoleSerializer()


class CustomerStatementRowSerializer(serializers.Serializer):
    date = serializers.CharField()
    type = serializers.CharField()
    ref = serializers.CharField()
    link = serializers.CharField(required=False, allow_null=True)
    debit = serializers.IntegerField()
    credit = serializers.IntegerField()
    balance = serializers.IntegerField()


class CustomerStatementSerializer(serializers.Serializer):
    customer_id = serializers.IntegerField()
    customer = CustomerSerializer()
    rows = CustomerStatementRowSerializer(many=True)
    total_debit = serializers.IntegerField()
    total_credit = serializers.IntegerField()
    balance = serializers.IntegerField()


class StartOnlinePaymentInputSerializer(serializers.Serializer):
    customer_id = serializers.IntegerField(required=False)
    amount = serializers.IntegerField(min_value=1)
    provider = serializers.ChoiceField(choices=["qi", "zaincash"], default="qi", required=False)
    key = serializers.CharField(required=False, allow_blank=True)
    client_ref = serializers.CharField(required=False, allow_blank=True)
    return_url = serializers.CharField(required=False, default="/statement")


class OnlinePaymentSerializer(serializers.Serializer):
    payment_id = serializers.CharField()
    provider = serializers.ChoiceField(choices=["qi", "zaincash"], default="qi")
    gateway_id = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    customer_id = serializers.IntegerField()
    amount = serializers.IntegerField()
    status = serializers.ChoiceField(choices=["created", "success", "failed", "authentication_failed", "canceled"])
    form_url = serializers.CharField()
    created_at = serializers.CharField()
    by = PersonRoleSerializer()
    recorded_payment_id = serializers.IntegerField(required=False, allow_null=True)
    note = serializers.CharField(required=False, allow_null=True, allow_blank=True)


class AllocationPreviewSerializer(serializers.Serializer):
    invoice_id = serializers.IntegerField()
    number = serializers.CharField()
    amount = serializers.IntegerField()
    remaining = serializers.IntegerField()
