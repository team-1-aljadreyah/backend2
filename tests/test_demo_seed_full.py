"""End-to-end tests for full demo seeding, idempotency, strict validation, safety guards, and clean rollback."""
from __future__ import annotations

import json
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings

from apps.accounts.models import User
from apps.catalog.models import Category, Item
from apps.common.models import DemoSeedRecord
from apps.customers.models import Customer
from apps.invoices.models import Invoice
from apps.orders.models import Order
from apps.warehouses.models import Warehouse


@pytest.mark.django_db
def test_seed_demo_production_guard(monkeypatch) -> None:
    monkeypatch.setenv("DJANGO_ENV", "production")
    with pytest.raises(CommandError, match="production"):
        call_command("seed_demo", "--profile", "full")


@pytest.mark.django_db
def test_seed_demo_clear_requires_explicit_confirmation(monkeypatch) -> None:
    monkeypatch.setenv("DJANGO_ENV", "testing")
    with pytest.raises(CommandError, match="confirm-delete-demo-data"):
        call_command("seed_demo", "--clear-demo-data")


@pytest.mark.django_db
def test_seed_demo_full_dry_run_does_not_write(monkeypatch) -> None:
    monkeypatch.setenv("DJANGO_ENV", "testing")
    out = StringIO()
    call_command("seed_demo", "--profile", "full", "--with-media", "--dry-run", stdout=out)
    payload = json.loads(out.getvalue())
    assert payload["dry_run"] is True
    assert payload["profile"] == "full"
    assert User.objects.count() == 0
    assert Item.objects.count() == 0
    assert Order.objects.count() == 0


@pytest.mark.django_db(transaction=True)
def test_seed_demo_full_idempotent_validation_and_safe_clear(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("DJANGO_ENV", "testing")
    with override_settings(MEDIA_ROOT=tmp_path):
        out1 = StringIO()
        call_command("seed_demo", "--profile", "full", "--with-media", stdout=out1)
        res1 = json.loads(out1.getvalue())
        assert res1["profile"] == "full"
        assert res1["already_seeded"] is False
        assert Warehouse.objects.filter(active=True).count() >= 5
        assert Item.objects.count() >= 90
        assert User.objects.filter(deleted=False).count() >= 65
        assert Customer.objects.count() >= 50
        assert Order.objects.count() >= 100
        assert Invoice.objects.count() >= 75

        # Re-run must be strictly idempotent
        out2 = StringIO()
        call_command("seed_demo", "--profile", "full", "--with-media", stdout=out2)
        res2 = json.loads(out2.getvalue())
        assert res2["already_seeded"] is True
        assert res2["counts"] == res1["counts"]

        # Strict validation command must pass cleanly
        val_out = StringIO()
        call_command("validate_demo_data", "--strict-full", stdout=val_out)
        val_payload = json.loads(val_out.getvalue())
        assert val_payload["status"] == "PASSED"
        assert val_payload["failures"] == []

        # Create a non-demo category and item that must survive `--clear-demo-data`
        manual_cat = Category.objects.create(name="تصنيف غير تجريبي", name_en="Manual Non-Demo Cat")
        manual_item = Item.objects.create(
            name="صنف يدوي محفوظ",
            name_en="Manual Preserved Item",
            category=manual_cat.name,
            pieces_per_carton=12,
        )

        call_command("seed_demo", "--clear-demo-data", "--confirm-delete-demo-data", stdout=StringIO())
        assert DemoSeedRecord.objects.count() == 0
        assert Order.objects.count() == 0
        assert Customer.objects.count() == 0
        assert Category.objects.filter(id=manual_cat.id).exists() is True
        assert Item.objects.filter(id=manual_item.id).exists() is True
