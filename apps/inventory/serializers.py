"""Serializers for Batch, ShelfRow, DamagedRecord."""
from __future__ import annotations

from datetime import date
from typing import Any

from rest_framework import serializers

from apps.accounts.models import SystemSetting
from apps.accounts.permissions import sees_cost
from apps.common.utils import baghdad_today
from apps.inventory.models import Batch, DamagedInventory


def compute_batch_status(expiry_date: date, *, today: date | None = None, near_days: int | None = None) -> str:
    today = today or baghdad_today()
    if expiry_date < today:
        return "expired"
    if near_days is None:
        near_days = SystemSetting.get_solo().near_expiry_days
    if (expiry_date - today).days <= near_days:
        return "near_expiry"
    return "good"


def serialize_batch(batch: Batch, *, actor: Any, today: date | None = None, near_days: int | None = None) -> dict[str, Any]:
    held_obj = None
    if batch.held_at is not None:
        held_obj = {
            "reason": batch.held_reason,
            "by": {"id": batch.held_by_id or 0, "name": batch.held_by.name if batch.held_by else ""},
            "at": batch.held_at.isoformat(),
        }
    data: dict[str, Any] = {
        "id": batch.id,
        "item_id": batch.item_id,
        "warehouse_id": batch.warehouse_id,
        "receipt_id": batch.receipt_id,
        "qty_received": batch.qty_received,
        "qty_left": batch.qty_left,
        "expiry_date": batch.expiry_date.isoformat(),
        "received_at": batch.received_at.isoformat(),
        "received_by": {
            "id": batch.receipt.created_by_id or 0 if batch.receipt else 0,
            "name": batch.receipt.created_by.name if batch.receipt and batch.receipt.created_by else "",
        },
        "status": compute_batch_status(batch.expiry_date, today=today, near_days=near_days),
        "held": held_obj,
    }
    if sees_cost(actor):
        data["purchase_price"] = batch.purchase_price
    return data


def serialize_damaged(rec: DamagedInventory) -> dict[str, Any]:
    return {
        "id": rec.id,
        "warehouse_id": rec.warehouse_id,
        "item_id": rec.item_id,
        "batch_id": rec.batch_id,
        "pieces": rec.pieces,
        "source": rec.source,
        "ref": rec.ref,
        "reason": rec.reason,
        "created_at": rec.created_at.isoformat(),
    }


# ==========================================
# DRF Serializers for OpenAPI documentation
# ==========================================

class PersonSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    name = serializers.CharField()


class BatchHeldSerializer(serializers.Serializer):
    reason = serializers.CharField()
    at = serializers.CharField()
    by = PersonSerializer()


class BatchSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    item_id = serializers.IntegerField()
    warehouse_id = serializers.IntegerField()
    expiry_date = serializers.CharField()
    qty_received = serializers.IntegerField()
    qty_left = serializers.IntegerField()
    purchase_price = serializers.IntegerField(required=False)
    received_at = serializers.CharField()
    received_by = PersonSerializer(required=False)
    receipt_id = serializers.IntegerField(required=False, allow_null=True)
    status = serializers.ChoiceField(choices=["good", "near_expiry", "expired"])
    held = BatchHeldSerializer(required=False, allow_null=True)


class BatchHoldInputSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=300)


class BatchTraceSerializer(serializers.Serializer):
    customer_id = serializers.IntegerField()
    customer = serializers.CharField()
    phone = serializers.CharField()
    invoice_id = serializers.IntegerField()
    invoice_number = serializers.CharField()
    date = serializers.CharField()
    pieces = serializers.IntegerField()
    returned = serializers.IntegerField()


class DamagedInventorySerializer(serializers.Serializer):
    id = serializers.IntegerField()
    warehouse_id = serializers.IntegerField()
    item_id = serializers.IntegerField()
    batch_id = serializers.IntegerField(allow_null=True)
    pieces = serializers.IntegerField()
    source = serializers.CharField()
    ref = serializers.CharField(allow_blank=True)
    reason = serializers.CharField(allow_blank=True)
    created_at = serializers.CharField()
