"""Idempotent development & staging demo data seeder."""
from __future__ import annotations

import base64
import io
import json
import os
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from PIL import Image

from apps.accounts.models import PriceTier, Role, SystemSetting, User
from apps.catalog.models import Category, Item
from apps.catalog.services import create_item
from apps.chat.models import ChatMessage
from apps.chat.services import send_customer_chat_message, send_my_chat_message
from apps.common.utils import baghdad_today, decrypt_secret
from apps.customers.models import Customer, CustomerStatus, ShopMembership, SpecialPrice
from apps.deliveries.models import Delivery, RepLocation, Vehicle
from apps.deliveries.services import deliver_order, dispatch_delivery
from apps.exchange_rates.models import UsdRate
from apps.expenses.models import Expense
from apps.expenses.services import create_expense
from apps.invoices.models import Invoice
from apps.invoices.services import create_return
from apps.orders.models import Order
from apps.orders.services import accept_order, create_order, prepare_order
from apps.payments.services import create_payment
from apps.receipts.models import Receipt
from apps.receipts.services import create_receipt
from apps.supply_orders.models import SupplyOrder
from apps.supply_orders.services import create_supply_order
from apps.warehouses.models import Warehouse


def _demo_png_data_url() -> str:
    buf = io.BytesIO()
    Image.new("RGB", (32, 32), color=(40, 120, 90)).save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def _get_or_create_user(
    *,
    phone: str,
    password: str,
    name: str,
    role: str,
    warehouse: Warehouse | None = None,
    rep_kinds: list[str] | None = None,
    rep_categories: list[str] | None = None,
) -> tuple[User, bool]:
    existing = User.objects.filter(phone=phone).first()
    if existing:
        return existing, False
    user = User.objects.create_user(
        phone=phone,
        password=password,
        name=name,
        role=role,
        warehouse=warehouse,
        rep_kinds=rep_kinds or [],
        rep_categories=rep_categories or [],
    )
    return user, True


class Command(BaseCommand):
    help = "Seed idempotent demo dataset for local development and QA (forbidden in production)."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--password",
            default=os.getenv("DEMO_SEED_PASSWORD", "Demo12345!"),
            help="Password assigned to newly created mini demo accounts (ignored for existing users).",
        )
        parser.add_argument(
            "--profile",
            choices=("mini", "full"),
            default="mini",
            help="Dataset profile: 'mini' (lightweight smoke/unit default) or 'full' (realistic 96-item/70-user ERP).",
        )
        parser.add_argument(
            "--with-media",
            action="store_true",
            help="Generate and bind WebP images + seed_assets/asset_manifest.json.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Preview planned actions and exit without writing to the database or storage.",
        )
        parser.add_argument(
            "--clear-demo-data",
            action="store_true",
            help="Delete ONLY records tracked in DemoSeedRecord (requires --confirm-delete-demo-data).",
        )
        parser.add_argument(
            "--confirm-delete-demo-data",
            action="store_true",
            help="Explicit safety confirmation required alongside --clear-demo-data.",
        )

    def handle(self, *args, **options) -> None:
        env = os.getenv("DJANGO_ENV", "").strip().lower()
        mod = os.getenv("DJANGO_SETTINGS_MODULE", "").strip().lower()
        cfg_mod = str(getattr(settings, "SETTINGS_MODULE", "")).strip().lower()
        is_test_settings = "testing" in mod or "testing" in cfg_mod or env == "testing"
        if (
            env == "production"
            or "production" in mod
            or "production" in cfg_mod
            or (not settings.DEBUG and not is_test_settings and env not in ("development", "staging"))
        ):
            raise CommandError("Refusing to execute seed_demo in a production or non-debug environment.")

        profile = options["profile"]
        dry_run = bool(options["dry_run"])
        with_media = bool(options["with_media"])
        clear_flag = bool(options["clear_demo_data"])
        confirm_delete = bool(options["confirm_delete_demo_data"])
        allow_demo_seed = os.getenv("ALLOW_DEMO_SEED", "").strip().lower() in ("1", "true", "yes")

        if (profile == "full" or clear_flag) and not is_test_settings and not allow_demo_seed:
            raise CommandError("ALLOW_DEMO_SEED=true is required in .env before running full demo seeding or deletion.")

        if clear_flag:
            if not confirm_delete:
                raise CommandError("Refusing destructive cleanup: pass --confirm-delete-demo-data alongside --clear-demo-data.")
            if dry_run:
                from apps.common.models import DemoSeedRecord

                self.stdout.write(
                    json.dumps(
                        {"dry_run": True, "action": "clear_demo_data", "tracked_records": DemoSeedRecord.objects.count()},
                        ensure_ascii=False,
                        indent=2,
                    )
                )
                return
            from apps.common.demo_seed import clear_demo_data

            deleted = clear_demo_data(delete_media=True)
            self.stdout.write(self.style.SUCCESS(f"Cleared tracked demo records: {json.dumps(deleted, ensure_ascii=False)}"))
            if profile != "full":
                return

        if profile == "full":
            from apps.common.demo_seed import run_full_seed

            result = run_full_seed(with_media=with_media, dry_run=dry_run)
            self.stdout.write(self.style.SUCCESS(json.dumps(result, ensure_ascii=False, indent=2)))
            return

        if dry_run:
            self.stdout.write(json.dumps({"profile": "mini", "dry_run": True}, indent=2))
            return

        password = options["password"]
        summary = self._seed(password=password)
        self.stdout.write(
            self.style.SUCCESS(
                f"Demo seed complete: warehouses={summary['warehouses']}, items={summary['items']}, "
                f"customers={summary['customers']}, orders={summary['orders']}, invoices={summary['invoices']}."
            )
        )

    @transaction.atomic
    def _seed(self, *, password: str) -> dict[str, int]:
        setting = SystemSetting.get_solo()
        today = baghdad_today()

        # 1. Warehouses & Price Tiers
        wh1, _ = Warehouse.objects.get_or_create(
            code="WH-KARKH",
            defaults={
                "name": "المخزن الرئيسي - الكرخ",
                "name_en": "Main Warehouse - Karkh",
                "capacity": 2000,
                "lat": Decimal("33.315200"),
                "lng": Decimal("44.366100"),
                "active": True,
            },
        )
        if wh1.lat is None or wh1.lng is None:
            wh1.capacity = wh1.capacity or 2000
            wh1.lat = Decimal("33.315200")
            wh1.lng = Decimal("44.366100")
            wh1.save(update_fields=["capacity", "lat", "lng"])
        wh2, _ = Warehouse.objects.get_or_create(
            code="WH-RUSAFA",
            defaults={
                "name": "مخزن الرصافة",
                "name_en": "Rusafa Warehouse",
                "capacity": 1500,
                "lat": Decimal("33.338500"),
                "lng": Decimal("44.421000"),
                "active": True,
            },
        )
        if wh2.lat is None or wh2.lng is None:
            wh2.capacity = wh2.capacity or 1500
            wh2.lat = Decimal("33.338500")
            wh2.lng = Decimal("44.421000")
            wh2.save(update_fields=["capacity", "lat", "lng"])
        tier1, _ = PriceTier.objects.get_or_create(
            name="جملة",
            defaults={"name_en": "Wholesale", "sort_order": 1},
        )
        tier2, _ = PriceTier.objects.get_or_create(
            name="نصف جملة",
            defaults={"name_en": "Semi-Wholesale", "sort_order": 2},
        )

        # 2. Demo Users across all roles
        mgr, _ = _get_or_create_user(phone="07701000001", password=password, name="أحمد المدير", role=Role.MANAGER)
        acct, _ = _get_or_create_user(phone="07701000002", password=password, name="سارة المحاسبة", role=Role.ACCOUNTANT)
        keeper1, _ = _get_or_create_user(
            phone="07701000003", password=password, name="علي أمين الكرخ", role=Role.WAREHOUSE, warehouse=wh1
        )
        keeper2, _ = _get_or_create_user(
            phone="07701000004", password=password, name="كرار أمين الرصافة", role=Role.WAREHOUSE, warehouse=wh2
        )
        _get_or_create_user(
            phone="07701000005", password=password, name="مصطفى موظف المخزن", role=Role.STAFF, warehouse=wh1
        )
        sales_rep, _ = _get_or_create_user(
            phone="07701000006",
            password=password,
            name="حسين المندوب",
            role=Role.REP,
            rep_kinds=["sales", "delivery"],
            rep_categories=["زيوت", "مشروبات", "حبوب"],
        )
        driver, _ = _get_or_create_user(
            phone="07701000007",
            password=password,
            name="زيد السائق",
            role=Role.REP,
            rep_kinds=["delivery"],
        )
        shop_user, _ = _get_or_create_user(phone="07701000008", password=password, name="عمر التاجر", role=Role.SHOP)

        UsdRate.objects.update_or_create(
            date=today,
            defaults={"rate": setting.usd_rate or Decimal("1500.00"), "by": mgr},
        )

        # 3. Categories & Catalog Items
        categories = [
            ("زيوت", "Oils", 1),
            ("حبوب", "Grains", 2),
            ("مشروبات", "Beverages", 3),
        ]
        for cname, cname_en, order_idx in categories:
            Category.objects.get_or_create(name=cname, defaults={"name_en": cname_en, "sort_order": order_idx})

        catalog_specs = [
            {
                "barcode": "ALN-DEMO-1001",
                "name": "زيت زيتون البكر 1 لتر",
                "name_en": "Extra Virgin Olive Oil 1L",
                "category": "زيوت",
                "pieces_per_carton": 12,
                "min_stock": 60,
                "commission_percent": "2.50",
                "prices": [
                    {"tier_id": tier1.id, "carton_price": 60000, "piece_price": 5200},
                    {"tier_id": tier2.id, "carton_price": 64000, "piece_price": 5500},
                ],
            },
            {
                "barcode": "ALN-DEMO-1002",
                "name": "أرز بسمتي هندي 5 كغم",
                "name_en": "Basmati Rice 5KG",
                "category": "حبوب",
                "pieces_per_carton": 4,
                "min_stock": 40,
                "commission_percent": "1.50",
                "prices": [
                    {"tier_id": tier1.id, "carton_price": 48000, "piece_price": 12500},
                    {"tier_id": tier2.id, "carton_price": 50000, "piece_price": 13000},
                ],
            },
            {
                "barcode": "ALN-DEMO-1003",
                "name": "عصير فواكه مشكل 250 مل",
                "name_en": "Mixed Fruit Juice 250ml",
                "category": "مشروبات",
                "pieces_per_carton": 24,
                "min_stock": 120,
                "commission_percent": "3.00",
                "prices": [
                    {"tier_id": tier1.id, "carton_price": 18000, "piece_price": 800},
                    {"tier_id": tier2.id, "carton_price": 19500, "piece_price": 850},
                ],
            },
            {
                "barcode": "ALN-DEMO-1004",
                "name": "شاي سيلاني فاخر 500 غم",
                "name_en": "Premium Ceylon Tea 500g",
                "category": "مشروبات",
                "pieces_per_carton": 12,
                "min_stock": 50,
                "commission_percent": "2.00",
                "prices": [
                    {"tier_id": tier1.id, "carton_price": 36000, "piece_price": 3200},
                    {"tier_id": tier2.id, "carton_price": 38000, "piece_price": 3400},
                ],
            },
        ]
        items_by_code: dict[str, Item] = {}
        for spec in catalog_specs:
            existing_item = Item.objects.filter(barcode=spec["barcode"]).first()
            if existing_item:
                if existing_item.commission_percent == Decimal("0.00"):
                    existing_item.commission_percent = Decimal(spec["commission_percent"])
                    existing_item.save(update_fields=["commission_percent"])
                items_by_code[spec["barcode"]] = existing_item
            else:
                created = create_item(actor=mgr, data=spec)
                items_by_code[spec["barcode"]] = Item.objects.get(id=created["id"])

        # 4. Customers & Multi-shop membership + SpecialPrice
        addr1 = {
            "label": "الفرع الرئيسي - المنصور",
            "details": "شارع 14 رمضان - قرب الساعة",
            "phone": "07701000008",
            "street": "14 رمضان",
            "landmark": "قرب الساعة",
            "lat": 33.3128,
            "lng": 44.3492,
        }
        addr2 = {
            "label": "فرع الكرادة",
            "details": "شارع 62 - ساحة الواثق",
            "phone": "07701000008",
            "street": "62",
            "landmark": "ساحة الواثق",
            "lat": 33.3014,
            "lng": 44.4165,
        }
        cust1, _ = Customer.objects.get_or_create(
            phone="07701000008",
            name="أسواق النهرين - المنصور",
            defaults={
                "owner_name": "عمر التاجر",
                "city": "بغداد",
                "area": "المنصور",
                "street": "14 رمضان",
                "landmark": "قرب الساعة",
                "address_text": "الفرع الرئيسي - المنصور - 14 رمضان - قرب الساعة",
                "address_info": addr1,
                "lat": Decimal("33.312800"),
                "lng": Decimal("44.349200"),
                "tier": tier1,
                "rep": sales_rep,
                "debt_limit": 2_000_000,
                "status": CustomerStatus.ACTIVE,
                "active": True,
                "approved_by": mgr,
            },
        )
        if not cust1.address_info:
            cust1.address_info = addr1
            cust1.address_text = "الفرع الرئيسي - المنصور - 14 رمضان - قرب الساعة"
            cust1.lat = Decimal("33.312800")
            cust1.lng = Decimal("44.349200")
            cust1.save(update_fields=["address_info", "address_text", "lat", "lng"])
        cust2, _ = Customer.objects.get_or_create(
            phone="07701000008",
            name="أسواق النهرين - فرع الكرادة",
            defaults={
                "owner_name": "عمر التاجر",
                "city": "بغداد",
                "area": "الكرادة",
                "street": "62",
                "landmark": "ساحة الواثق",
                "address_text": "فرع الكرادة - 62 - ساحة الواثق",
                "address_info": addr2,
                "lat": Decimal("33.301400"),
                "lng": Decimal("44.416500"),
                "tier": tier2,
                "rep": sales_rep,
                "debt_limit": 1_000_000,
                "status": CustomerStatus.ACTIVE,
                "active": True,
                "approved_by": mgr,
            },
        )
        if not cust2.address_info:
            cust2.address_info = addr2
            cust2.address_text = "فرع الكرادة - 62 - ساحة الواثق"
            cust2.lat = Decimal("33.301400")
            cust2.lng = Decimal("44.416500")
            cust2.save(update_fields=["address_info", "address_text", "lat", "lng"])
        Customer.objects.get_or_create(
            phone="07701000009",
            name="ماركت البصرة المركزي",
            defaults={
                "owner_name": "سامر البصري",
                "city": "البصرة",
                "area": "العشار",
                "street": "الوطن",
                "landmark": "سوق البصرة",
                "tier": tier1,
                "rep": sales_rep,
                "debt_limit": 1_500_000,
                "status": CustomerStatus.ACTIVE,
                "active": True,
                "approved_by": mgr,
            },
        )
        Customer.objects.get_or_create(
            phone="07701000010",
            name="بقالة دجلة الجديدة",
            defaults={
                "owner_name": "ليث علي",
                "city": "بغداد",
                "area": "زيونة",
                "street": "الربيعي",
                "landmark": "مول زيونة",
                "tier": tier2,
                "rep": sales_rep,
                "debt_limit": 0,
                "status": CustomerStatus.PENDING,
                "active": False,
            },
        )

        ShopMembership.objects.get_or_create(
            user=shop_user,
            customer=cust1,
            defaults={"is_primary": True, "is_active_shop": True},
        )
        ShopMembership.objects.get_or_create(
            user=shop_user,
            customer=cust2,
            defaults={"is_primary": False, "is_active_shop": False},
        )
        SpecialPrice.objects.update_or_create(
            customer=cust1,
            item=items_by_code["ALN-DEMO-1001"],
            defaults={"carton_price": 58000, "piece_price": 5000},
        )

        # 5. Receipts & Stock Batches
        if not Receipt.objects.filter(supplier="شركة النهرين للاستيراد [DEMO]").exists():
            far_exp = (today + timedelta(days=180)).isoformat()
            near_exp = (today + timedelta(days=12)).isoformat()
            create_receipt(
                actor=keeper1,
                data={
                    "warehouse_id": wh1.id,
                    "supplier": "شركة النهرين للاستيراد [DEMO]",
                    "lines": [
                        {
                            "item_id": items_by_code["ALN-DEMO-1001"].id,
                            "unit": "carton",
                            "qty": 25,
                            "expiry_date": far_exp,
                            "purchase_price": 4200,
                        },
                        {
                            "item_id": items_by_code["ALN-DEMO-1002"].id,
                            "unit": "carton",
                            "qty": 30,
                            "expiry_date": far_exp,
                            "purchase_price": 10000,
                        },
                        {
                            "item_id": items_by_code["ALN-DEMO-1003"].id,
                            "unit": "carton",
                            "qty": 15,
                            "expiry_date": near_exp,
                            "purchase_price": 600,
                        },
                        {
                            "item_id": items_by_code["ALN-DEMO-1004"].id,
                            "unit": "carton",
                            "qty": 20,
                            "expiry_date": far_exp,
                            "purchase_price": 2500,
                        },
                    ],
                },
            )
            create_receipt(
                actor=keeper2,
                data={
                    "warehouse_id": wh2.id,
                    "supplier": "شركة النهرين للاستيراد - الرصافة [DEMO]",
                    "lines": [
                        {
                            "item_id": items_by_code["ALN-DEMO-1001"].id,
                            "unit": "carton",
                            "qty": 10,
                            "expiry_date": far_exp,
                            "purchase_price": 4200,
                        },
                        {
                            "item_id": items_by_code["ALN-DEMO-1003"].id,
                            "unit": "carton",
                            "qty": 25,
                            "expiry_date": far_exp,
                            "purchase_price": 600,
                        },
                    ],
                },
            )

        # 6. Delivered Order -> Invoice -> Payment (FIFO) -> ReturnDoc
        if not Order.objects.filter(client_ref="demo-seed-order-delivered").exists():
            ord1 = create_order(
                actor=sales_rep,
                data={
                    "client_ref": "demo-seed-order-delivered",
                    "customer_id": cust1.id,
                    "warehouse_id": wh1.id,
                    "lines": [
                        {"item_id": items_by_code["ALN-DEMO-1001"].id, "unit": "carton", "qty": 3},
                        {"item_id": items_by_code["ALN-DEMO-1002"].id, "unit": "carton", "qty": 2},
                    ],
                },
            )
            if ord1["status"] == "pending":
                accept_order(actor=mgr, order_id=ord1["id"])
            prepare_order(actor=keeper1, order_id=ord1["id"], warehouses_map={})
            dlv = Delivery.objects.select_related("rep").get(order_id=ord1["id"])
            del_actor = dlv.rep or driver
            dispatch_delivery(actor=del_actor, code=f"NAHRAIN:DLV:{dlv.code}")
            pin = decrypt_secret(dlv.pin_encrypted)
            deliver_order(
                actor=del_actor,
                order_id=ord1["id"],
                data={"pin": pin, "photo": _demo_png_data_url()},
            )
            inv1 = Invoice.objects.get(order_id=ord1["id"])
            create_payment(
                actor=acct,
                data={
                    "client_ref": "demo-seed-payment-1",
                    "customer_id": cust1.id,
                    "amount": min(150_000, inv1.total),
                    "method": "cash",
                    "note": "دفعة أولية [DEMO]",
                },
            )
            create_return(
                actor=acct,
                data={
                    "client_ref": "demo-seed-return-1",
                    "invoice_id": inv1.id,
                    "note": "إرجاع قطعة واحدة سليمة [DEMO]",
                    "lines": [
                        {
                            "item_id": items_by_code["ALN-DEMO-1001"].id,
                            "unit": "piece",
                            "qty": 1,
                            "condition": "good",
                        }
                    ],
                },
            )

        # 7. Active Out-for-Delivery & Pending Approval Orders
        if not Order.objects.filter(client_ref="demo-seed-order-out").exists():
            ord2 = create_order(
                actor=sales_rep,
                data={
                    "client_ref": "demo-seed-order-out",
                    "customer_id": cust1.id,
                    "warehouse_id": wh1.id,
                    "lines": [
                        {"item_id": items_by_code["ALN-DEMO-1004"].id, "unit": "carton", "qty": 2},
                    ],
                },
            )
            if ord2["status"] == "pending":
                accept_order(actor=mgr, order_id=ord2["id"])
            prepare_order(actor=keeper1, order_id=ord2["id"], warehouses_map={})
            dlv2 = Delivery.objects.select_related("rep").get(order_id=ord2["id"])
            dispatch_delivery(actor=dlv2.rep or driver, code=f"NAHRAIN:DLV:{dlv2.code}")

        if not Order.objects.filter(client_ref="demo-seed-order-pending").exists():
            create_order(
                actor=shop_user,
                data={
                    "client_ref": "demo-seed-order-pending",
                    "warehouse_id": wh1.id,
                    "lines": [
                        {"item_id": items_by_code["ALN-DEMO-1003"].id, "unit": "carton", "qty": 2},
                    ],
                },
            )

        # 8. Support Chat Conversation & Supply Order & Rep Live GPS Locations
        if not ChatMessage.objects.filter(customer=cust1).exists():
            send_my_chat_message(actor=shop_user, text="السلام عليكم، هل يتوفر شحن إضافي يوم غد؟")
            send_customer_chat_message(
                actor=mgr,
                customer_id=cust1.id,
                text="وعليكم السلام، نعم التوصيل متاح صباح غد الساعة 9.",
            )

        if not SupplyOrder.objects.filter(supplier="مورد البصرة للمواد الغذائية [DEMO]").exists():
            create_supply_order(
                actor=mgr,
                data={
                    "warehouse_id": wh1.id,
                    "supplier": "مورد البصرة للمواد الغذائية [DEMO]",
                    "supplier_phone": "07712223344",
                    "expected_date": (today + timedelta(days=3)).isoformat(),
                    "note": "طلب توريد تجريبي [DEMO]",
                    "lines": [
                        {
                            "item_id": items_by_code["ALN-DEMO-1001"].id,
                            "unit": "carton",
                            "qty": 10,
                            "price": 49000,
                        },
                        {
                            "item_id": items_by_code["ALN-DEMO-1004"].id,
                            "unit": "carton",
                            "qty": 15,
                            "price": 29000,
                        },
                    ],
                },
            )

        RepLocation.objects.update_or_create(
            rep=driver,
            defaults={"lat": Decimal("33.320400"), "lng": Decimal("44.374100")},
        )
        RepLocation.objects.update_or_create(
            rep=sales_rep,
            defaults={"lat": Decimal("33.310500"), "lng": Decimal("44.355200")},
        )

        # 9. Fleet Vehicles & Sample Operating Expenses
        v1, _ = Vehicle.objects.get_or_create(
            name="فان الكرخ 1",
            defaults={
                "plate": "بغداد 4821 أ",
                "capacity_kg": 1200,
                "driver": driver,
                "active": True,
            },
        )
        Vehicle.objects.get_or_create(
            name="شاحنة الرصافة 2",
            defaults={
                "plate": "بغداد 9150 ب",
                "capacity_kg": 3500,
                "driver": sales_rep,
                "active": True,
            },
        )
        if not Expense.objects.filter(paid_to="محطة وقود الكرخ [DEMO]").exists():
            create_expense(
                actor=mgr,
                data={
                    "category": "وقود وتوصيل",
                    "amount": 75000,
                    "method": "cash",
                    "paid_to": "محطة وقود الكرخ [DEMO]",
                    "date": today.isoformat(),
                    "warehouse_id": wh1.id,
                    "vehicle_id": v1.id,
                    "note": "وقود جولات الأسبوع [DEMO]",
                },
            )
        if not Expense.objects.filter(paid_to="شركة كهرباء المنصور [DEMO]").exists():
            create_expense(
                actor=acct,
                data={
                    "category": "كهرباء ومولدات",
                    "amount": 320000,
                    "method": "transfer",
                    "paid_to": "شركة كهرباء المنصور [DEMO]",
                    "date": today.isoformat(),
                    "warehouse_id": wh1.id,
                    "note": "فاتورة تشغيل المولد [DEMO]",
                },
            )

        return {
            "warehouses": Warehouse.objects.count(),
            "vehicles": Vehicle.objects.count(),
            "items": Item.objects.count(),
            "customers": Customer.objects.count(),
            "orders": Order.objects.count(),
            "invoices": Invoice.objects.count(),
            "expenses": Expense.objects.count(),
        }
