"""70 synthetic ERP users across all roles, 52 Iraqi wholesale customers/shops, 5 vehicles, and GPS trails."""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from django.conf import settings

from apps.accounts.models import LoginAttempt, PriceTier, Role, User
from apps.accounts.services import create_user_by_manager, set_user_profile
from apps.catalog.models import Item
from apps.common.demo_seed.catalog_data import CATEGORIES
from apps.common.demo_seed.registry import frozen_now, get_seeded, register_seed
from apps.common.utils import baghdad_now
from apps.customers.models import Customer, CustomerStatus, ShopMembership, SpecialPrice
from apps.customers.services import create_customer, reject_customer
from apps.deliveries.models import RepLocation, Vehicle
from apps.deliveries.services import save_vehicle
from apps.warehouses.models import Warehouse
from apps.warehouses.services import set_warehouse_cover

DEMO_PASSWORDS: dict[str, str] = {
    "manager": "AlNahrain!Mgr#2026",
    "accountant": "AlNahrain!Acc#2026",
    "warehouse": "AlNahrain!Wh#2026",
    "staff": "AlNahrain!Stf#2026",
    "rep": "AlNahrain!Rep#2026",
    "shop": "AlNahrain!Shop#2026",
}

ACCOUNTANT_SPECS = [
    ("usr:acc:1", "07701000011", "زينب عبد الكريم المياحي", True, "prof_senior_acc", 1),
    ("usr:acc:2", "07701000012", "حسين علي جواد الصافي", True, "prof_jr_acc", 2),
]

KEEPER_SPECS = [
    ("usr:wh:1", "07701000021", "مرتضى قاسم كاظم الفتلاوي", 0, "prof_wh_supervisor", True),
    ("usr:wh:2", "07701000022", "علي فاضل هادي الكربلائي", 1, "prof_wh_keeper", True),
    ("usr:wh:3", "07701000023", "مصطفى رحيم نوري الياسري", 0, "prof_wh_keeper", False),
]

STAFF_NAMES = [
    "سجاد كريم حسن المسعودي",
    "منتظر حميد جاسم الطائي",
    "ياسر ماجد سلمان الجبوري",
    "ضياء ستار خضير الدليمي",
    "عمران طالب محمد الحسناوي",
    "باسم نجم عبد الله السعدون",
    "رافد عادل محسن الخفاجي",
    "غيث طه رزاق العبودي",
    "وليد شاكر محمود البياتي",
    "ليث سمير كاظم الموسوي",
]

REP_SPECS: list[tuple[str, str, str, list[str], list[str], int | None, bool, str | None]] = [
    # key, phone, name, kinds, categories, target, active, profile_id
    ("usr:rep:sales:1", "07701000041", "عمار هاشم رشيد الشمري", ["sales"], [], 58000000, True, "prof_sales_rep"),
    ("usr:rep:sales:2", "07701000042", "رعد فالح عبود الخالدي", ["sales"], [], 52000000, True, "prof_sales_rep"),
    ("usr:rep:sales:3", "07701000043", "حسام نزار مجيد الزبيدي", ["sales"], [CATEGORIES[0][0], CATEGORIES[1][0], CATEGORIES[3][0], CATEGORIES[5][0]], 64000000, True, "prof_sales_rep"),
    ("usr:rep:sales:4", "07701000044", "مهند صباح نعمة العامري", ["sales"], [CATEGORIES[4][0], CATEGORIES[6][0], CATEGORIES[7][0], CATEGORIES[8][0]], 45000000, True, "prof_sales_rep"),
    ("usr:rep:dlv:1", "07701000051", "قحطان خالد جليل التميمي", ["delivery"], [], None, True, "prof_dlv_rep"),
    ("usr:rep:dlv:2", "07701000052", "سلام مهدي شهاب الركابي", ["delivery"], [], None, True, "prof_dlv_rep"),
    ("usr:rep:dlv:3", "07701000053", "فراس طارق عبد الغني البهادلي", ["delivery"], [], None, True, "prof_dlv_rep"),
    ("usr:rep:dlv:4", "07701000054", "عصام جواد كاظم الكعبي", ["delivery"], [], None, True, "prof_dlv_rep"),
    ("usr:rep:both:1", "07701000055", "أنس عبد الرزاق ياسين المفرجي", ["sales", "delivery"], [], 38000000, True, None),
    ("usr:rep:disabled:1", "07701000056", "ناظم صادق إبراهيم الدراجي", ["sales"], [], 25000000, False, None),
]

VEHICLE_SPECS = [
    ("veh:1", "براد النهرين 1 — مخزن الصناعة", "Al-Nahrain Reefer 1 (Al-Sina'a)", "كربلاء 41208 أ", 4800),
    ("veh:2", "تريلة النهرين 2 — مخزن الصناعة", "Al-Nahrain Trunk Van 2 (Al-Sina'a)", "كربلاء 88514 ب", 6500),
    ("veh:3", "شاحنة النهرين 3 — مخزن الحر", "Al-Nahrain Express Truck 3 (Al-Hur)", "كربلاء 23190 ج", 3600),
    ("veh:4", "فان التوزيع 4 — مخزن الحر", "Distribution Van 4 (Al-Hur)", "كربلاء 19402 هـ", 3000),
    ("veh:5", "براد الخط السريع 5 — طوارئ", "Backup Reefer 5", "كربلاء 55091 د", 4200),
]

# 52 Synthetic Iraqi Shops across Karbala, Najaf, Baghdad, Hillah/Babil, Diwaniyah
SHOP_NAMES = [
    "أسواق الفرات المركزية", "هايبر ماركت الكوثر", "سوبرماركت النخيل الذهبي", "متجر روابي الحسين",
    "أسواق باب الخان للجملة", "مجمع الزهراء التجاري", "سوبرماركت السفير", "أسواق ضفاف الفرات",
    "متجر البركة للمواد الغذائية", "مركز الغدير للتسوق", "أسواق الندى العائلية", "سوبرماركت الأندلس",
    "أسواق المها الحديثة", "متجر دجلة الخير", "سوبرماركت النور الساطع", "أسواق الجود للجملة",
    "مجمع السبطين الاستهلاكي", "أسواق نبع السلام", "سوبرماركت القصر الأبيض", "متجر المروة الغذائي",
    "أسواق الكوفة الكبرى", "مركز الغري للتجارة", "سوبرماركت الأمير", "أسواق بوابة النجف",
    "متجر السلامة التجاري", "أسواق السهل الأخضر", "أسواق المنصور الذهبية", "هايبر ماركت اليرموك",
    "سوبرماركت السندباد", "أسواق جميلة الحديثة", "متجر الكرخ الكبير", "أسواق الزوراء المركزية",
    "سوبرماركت البياع", "أسواق الدورة العائلية", "متجر الرضوان للجملة", "مركز الرافدين للتسوق",
    "أسواق المأمون", "سوبرماركت القادسية", "متجر الحلة الحديث", "أسواق بابل المركزية",
    "سوبرماركت عشتار", "متجر الجنائن المعلقة", "أسواق شط الحلة", "سوبرماركت النسيج",
    "متجر المحاويل التجاري", "أسواق الديوانية الكبرى", "سوبرماركت السماوة (خط الوسط)",
    "أسواق الفجر الجديد", "متجر الشفق للتجارة", "سوبرماركت سنابل الخير", "أسواق الياسمين",
    "متجر النورس الغذائي",
]

OWNER_NAMES = [
    "حسين فاضل المعموري", "عباس كريم الزيادي", "محمود عبد الله الجبوري", "علي حسين الصالحي",
    "سعدون طارق العامري", "صادق مهدي الخفاجي", "مازن قاسم العكيلي", "أركان حيدر الساعدي",
    "كرار نوري الغراوي", "نزار سلمان البديري", "هيثم جواد الشويلي", "يوسف راضي الفرطوسي",
    "بهاء الدين حسن الوائلي", "صفاء لطيف الباوي", "حارث عماد المحمداوي", "قتيبة جمال العزاوي",
    "مؤمل رحيم الكعبي", "ستار جبار الشبلي", "إحسان محسن الحلفي", "فلاح حسن الربيعي",
    "عادل صباح الدليمي", "رائد نعيم التميمي", "حامد مجيد العبيدي", "وسام خليل العيساوي",
    "زيدون طه الحسني", "علاء ناصر الياسري", "جلال محمد الشريفي", "مثنى كامل الأوسي",
    "سنان عبد الرزاق القيسي", "أمجد عدنان السعدي", "ثامر هادي المشهداني", "منير عبد الكريم الصدر",
    "فائز فخري الجنابي", "نبيل شاكر الطائي", "أثير داود السامرائي", "حسنين علي الكرعاوي",
    "عماد طارق العتبي", "وليد خالد المندلاوي", "شهاب أحمد العاني", "مضر سامي الخالدي",
    "أسامة رشيد الحديدي", "تحسين عليوي البوكمال", "غسان حاتم الدراجي", "خالدون جاسم العقابي",
    "ماجد حميد الزهيري", "أكرم ياسين الهلالي", "مهيمن جليل المرسومي", "رغيد فؤاد البصري",
    "نافع كاظم الجعيفري", "ساهر نجم السعيدي", "أزهر طالب الفهداوي", "واثق حسين الحيدري",
]

AREA_PROFILES: list[tuple[str, str, str, str, float, float]] = [
    # city, governorate, area, landmark_prefix, base_lat, base_lng
    ("كربلاء", "كربلاء المقدسة", "حي الحسين", "قرب مستشفى الحسين العام", 32.6062, 44.0185),
    ("كربلاء", "كربلاء المقدسة", "حي العباس", "شارع السدرة التجاري", 32.6175, 44.0342),
    ("كربلاء", "كربلاء المقدسة", "طريق طويريج", "قرب سيطرة الإبراهيمية", 32.5890, 44.0720),
    ("كربلاء", "كربلاء المقدسة", "الحر الصغير", "سوق الحر المركزي", 32.6331, 43.9912),
    ("النجف", "النجف الأشرف", "حي الأمير", "شارع المثنى التجاري", 32.0124, 44.3420),
    ("النجف", "النجف الأشرف", "الكوفة", "قرب جسر الكوفة", 32.0365, 44.4022),
    ("النجف", "النجف الأشرف", "حي العدالة", "قرب جامعة الكوفة", 32.0281, 44.3610),
    ("بغداد", "بغداد", "المنصور", "شارع 14 رمضان", 33.3145, 44.3428),
    ("بغداد", "بغداد", "اليرموك", "قرب تقاطع الأردن", 33.2980, 44.3291),
    ("بغداد", "بغداد", "الكرادة", "ساحة كهرمانة", 33.3075, 44.4264),
    ("بغداد", "بغداد", "جميلة", "علاوي جميلة التجارية", 33.3688, 44.4240),
    ("الحلة", "بابل", "حي النسيج", "شارع 60 التجاري", 32.4750, 44.4265),
    ("الحلة", "بابل", "حي الجزائر", "قرب منتجع بابل", 32.4910, 44.4390),
    ("الديوانية", "الديوانية", "حي العروبة", "شارع الأطباء العام", 31.9885, 44.9255),
]


def _create_or_get_user(
    *,
    seed_key: str,
    actor_manager: User | None,
    phone: str,
    password: str,
    name: str,
    role: str,
    warehouse_id: int | None = None,
    rep_kinds: list[str] | None = None,
    rep_categories: list[str] | None = None,
    sales_target: int | None = None,
    active: bool = True,
    profile_id: str | None = None,
    last_login_days_ago: int | None = 1,
) -> User:
    u = get_seeded(seed_key, User) or User.objects.filter(phone=phone).first()
    now_dt = baghdad_now()
    if not u:
        created_at = now_dt - timedelta(days=90)
        with frozen_now(created_at):
            if actor_manager is None:
                u = User.objects.create_user(
                    phone=phone,
                    password=password,
                    name=name,
                    role=role,
                    active=active,
                    weak_password=False,
                )
            else:
                res = create_user_by_manager(
                    actor=actor_manager,
                    data={
                        "name": name,
                        "phone": phone,
                        "password": password,
                        "role": role,
                        "active": active,
                        "warehouse_id": warehouse_id,
                        "rep_kinds": rep_kinds or [],
                        "rep_categories": rep_categories or [],
                        **({"sales_target": sales_target} if sales_target is not None else {}),
                    },
                )
                u = User.objects.get(id=res["id"])
        if profile_id and actor_manager is not None and role != Role.MANAGER:
            set_user_profile(actor=actor_manager, user_id=u.id, profile_id=profile_id)
            u.refresh_from_db()
        if last_login_days_ago is not None and active:
            ll = now_dt - timedelta(days=last_login_days_ago, hours=(u.id % 9))
            u.last_login = ll
            u.save(update_fields=["last_login"])
            LoginAttempt.objects.create(
                phone=phone,
                user=u,
                ok=True,
                reason="",
                ip_address=f"10.10.{(u.id % 5) + 1}.{(u.id % 200) + 10}",
                created_at=ll,
            )
        elif not active:
            LoginAttempt.objects.create(
                phone=phone,
                user=u,
                ok=False,
                reason="stopped",
                ip_address="10.10.9.50",
            )
    register_seed(seed_key, u, extra={"role": role, "phone": phone})
    return u


def ensure_primary_manager() -> User:
    """Create or recover the main Manager account needed as actor for subsequent service calls."""
    return _create_or_get_user(
        seed_key="usr:mgr:1",
        actor_manager=None,
        phone="07701000001",
        password=DEMO_PASSWORDS["manager"],
        name="طارق عبد الجبار المنصوري",
        role=Role.MANAGER,
        active=True,
        last_login_days_ago=0,
    )


def seed_people_and_network(
    manager: User,
    *,
    warehouses: list[Warehouse],
    tiers: list[PriceTier],
    items: list[Item],
) -> dict[str, Any]:
    """Seed all 69 synthetic users, 52 Iraqi shops/customers, SpecialPrices, Vehicles, and RepLocations across the 2 warehouses."""
    now_dt = baghdad_now()
    all_users: list[User] = [manager]
    accountants: list[User] = []
    keepers: list[User] = []
    staff_users: list[User] = []
    sales_reps: list[User] = []
    delivery_reps: list[User] = []
    shop_users: list[User] = []

    # Accountants (2)
    for sk, phone, name, act, prof, login_ago in ACCOUNTANT_SPECS:
        u = _create_or_get_user(
            seed_key=sk,
            actor_manager=manager,
            phone=phone,
            password=DEMO_PASSWORDS["accountant"],
            name=name,
            role=Role.ACCOUNTANT,
            active=act,
            profile_id=prof,
            last_login_days_ago=login_ago,
        )
        accountants.append(u)
        all_users.append(u)

    # Warehouse Keepers (1 active keeper per warehouse: Al-Sina'a + Al-Hur, plus 1 inactive former keeper)
    for idx, (sk, phone, name, wh_idx, prof_id, is_act) in enumerate(KEEPER_SPECS, start=1):
        wh = warehouses[wh_idx]
        u = _create_or_get_user(
            seed_key=sk,
            actor_manager=manager,
            phone=phone,
            password=DEMO_PASSWORDS["warehouse"],
            name=name,
            role=Role.WAREHOUSE,
            warehouse_id=wh.id,
            active=is_act,
            profile_id=prof_id,
            last_login_days_ago=(idx % 3) if is_act else None,
        )
        keepers.append(u)
        all_users.append(u)

    # Temporary cover: Al-Hur keeper covers Al-Sina'a briefly
    if not warehouses[0].cover and len(keepers) >= 2:
        set_warehouse_cover(actor=manager, warehouse_id=warehouses[0].id, user_id=keepers[1].id, hours=12)

    # Warehouse Staff (10 — 5 in Al-Sina'a + 5 in Al-Hur; #9 read-only counter, #10 disabled)
    for idx in range(1, 11):
        wh = warehouses[0] if idx <= 5 else warehouses[1]
        is_active = idx != 10
        prof_id = "prof_wh_counter_readonly" if idx == 9 else ("prof_wh_picker" if (idx % 2 == 1 and is_active) else None)
        u = _create_or_get_user(
            seed_key=f"usr:staff:{idx}",
            actor_manager=manager,
            phone=f"077010000{30 + idx}",
            password=DEMO_PASSWORDS["staff"],
            name=STAFF_NAMES[idx - 1],
            role=Role.STAFF,
            warehouse_id=wh.id,
            active=is_active,
            profile_id=prof_id,
            last_login_days_ago=None if idx in (9, 10) else (idx % 5),
        )
        staff_users.append(u)
        all_users.append(u)

    # Sales & Delivery Reps (10 total: 4 sales, 4 delivery, 1 hybrid, 1 disabled)
    for idx, (sk, phone, name, kinds, cats, target, is_active, prof) in enumerate(REP_SPECS):
        u = _create_or_get_user(
            seed_key=sk,
            actor_manager=manager,
            phone=phone,
            password=DEMO_PASSWORDS["rep"],
            name=name,
            role=Role.REP,
            rep_kinds=kinds,
            rep_categories=cats,
            sales_target=target,
            active=is_active,
            profile_id=prof,
            last_login_days_ago=None if not is_active else (idx % 4),
        )
        if is_active and "sales" in kinds:
            sales_reps.append(u)
        if is_active and "delivery" in kinds:
            delivery_reps.append(u)
        all_users.append(u)

    # Security audit sample failed login attempts
    if not LoginAttempt.objects.filter(ok=False, reason="wrong").exists():
        LoginAttempt.objects.create(
            phone=sales_reps[0].phone,
            user=sales_reps[0],
            ok=False,
            reason="wrong",
            ip_address="10.10.22.41",
        )

    # 5 Fleet Vehicles (each assigned to a fleet driver)
    vehicles: list[Vehicle] = []
    for vidx, (sk, name, name_en, plate, cap) in enumerate(VEHICLE_SPECS):
        veh = get_seeded(sk, Vehicle) or Vehicle.objects.filter(plate=plate).first()
        if not veh:
            driver = delivery_reps[vidx % len(delivery_reps)] if delivery_reps else None
            res = save_vehicle(
                actor=manager,
                data={
                    "name": name,
                    "name_en": name_en,
                    "plate": plate,
                    "capacity_kg": cap,
                    "driver_id": driver.id if driver else None,
                    "active": True,
                },
            )
            veh = Vehicle.objects.get(id=res["id"])
        register_seed(sk, veh)
        vehicles.append(veh)

    # GPS breadcrumbs for all active delivery reps across Karbala/Najaf/Baghdad/Hillah highways
    route_coords = [
        (32.6160, 44.0249),  # Karbala downtown
        (32.0241, 44.3468),  # Najaf Kufa corridor
        (33.3092, 44.3661),  # Baghdad Karkh-Karrada
        (32.4780, 44.4290),  # Hillah city loop
        (32.3105, 44.2050),  # Karbala-Najaf highway
    ]
    for ridx, drep in enumerate(delivery_reps):
        lat, lng = route_coords[ridx % len(route_coords)]
        loc, created = RepLocation.objects.get_or_create(
            rep=drep,
            defaults={"lat": Decimal(str(lat)), "lng": Decimal(str(lng))},
        )
        if not created:
            loc.lat = Decimal(str(lat))
            loc.lng = Decimal(str(lng))
            loc.save(update_fields=["lat", "lng"])

    # 52 Customers / Shops (47 Active + 3 Pending + 2 Rejected) + 42 Shop Users (4 multi-shop owners!)
    customers: list[Customer] = []
    shop_types = ["هايبر ماركت", "سوبرماركت", "أسواق جملة", "موزع محلي", "تموين مطاعم"]
    for idx in range(1, 53):
        ckey = f"cust:{idx:03d}"
        phone = f"0780200{idx:04d}"
        city, gov, area, landmark, b_lat, b_lng = AREA_PROFILES[(idx - 1) % len(AREA_PROFILES)]
        lat = round(b_lat + ((idx % 7) - 3) * 0.0042, 6)
        lng = round(b_lng + ((idx % 5) - 2) * 0.0048, 6)
        shop_name = SHOP_NAMES[idx - 1]
        owner_name = OWNER_NAMES[idx - 1]
        tier = tiers[(idx - 1) % len(tiers)]
        rep = sales_reps[(idx - 1) % len(sales_reps)]

        # 1..47 active; 48..50 pending onboarding; 51..52 rejected
        is_pending = idx in (48, 49, 50)
        is_rejected = idx in (51, 52)
        debt_limit = (25000000 if idx <= 10 else 10000000 if idx <= 30 else 5000000) if not (is_pending or is_rejected) else 0

        cust = get_seeded(ckey, Customer) or Customer.objects.filter(phone=phone).first()
        if not cust:
            contract_dt = (baghdad_now() - timedelta(days=90 + (idx * 5))).date().isoformat()
            mahalla = 300 + (idx % 45)
            zuqaq = 10 + (idx % 30)
            dar = 1 + (idx % 80)
            with frozen_now(now_dt - timedelta(days=88)):
                res = create_customer(
                    actor=manager,
                    data={
                        "name": shop_name,
                        "owner_name": owner_name,
                        "phone": phone,
                        "city": city,
                        "area": area,
                        "street": f"شارع {area} التجاري رقم {(idx % 9) + 1}",
                        "landmark": landmark,
                        "address_info": {
                            "label": f"{gov} — {area}",
                            "details": f"{gov} - {area} - محلة {mahalla} زقاق {zuqaq} دار {dar}",
                            "phone": phone,
                            "street": f"شارع {area} التجاري رقم {(idx % 9) + 1}",
                            "building": f"بناية {(idx % 18) + 1}",
                            "floor": "الطابق الأرضي",
                            "landmark": landmark,
                            "lat": lat,
                            "lng": lng,
                        },
                        "lat": lat,
                        "lng": lng,
                        "tier_id": tier.id,
                        "rep_id": rep.id,
                        "debt_limit": debt_limit,
                        "active": not (is_pending or is_rejected),
                        "custom": {
                            "cf_cust_type": shop_types[(idx - 1) % len(shop_types)],
                            "cf_cust_gov": gov,
                            "cf_cust_contract": contract_dt,
                        },
                    },
                )
            cust = Customer.objects.get(id=res["id"])
        if is_pending and cust.status != CustomerStatus.PENDING:
            cust.status = CustomerStatus.PENDING
            cust.active = False
            cust.approved_by = None
            cust.save(update_fields=["status", "active", "approved_by"])
        elif is_rejected and cust.status != CustomerStatus.REJECTED:
            reject_customer(actor=manager, customer_id=cust.id, reason="بيانات السجل التجاري غير مكتملة")
            cust.refresh_from_db()
        register_seed(ckey, cust, extra={"city": city, "status": cust.status})
        customers.append(cust)

    # Link 42 Shop Owner Users to the 52 shops (first 4 owners manage 2 shops each)
    # Shop users 1..38 -> primary cust[0..37], with users 1..4 also linked to cust[38..41]
    # Shop users 39..40 -> cust[42..43] + [44..45], 41 -> pending cust[47], 42 -> cust[46]
    owner_mapping: list[tuple[int, int, list[int]]] = []
    for uidx in range(1, 39):
        secondary = [37 + uidx] if uidx <= 4 else []
        owner_mapping.append((uidx, uidx - 1, secondary))
    owner_mapping.append((39, 42, [44]))
    owner_mapping.append((40, 43, [45]))
    owner_mapping.append((41, 47, []))  # pending customer
    owner_mapping.append((42, 46, []))

    for uidx, primary_cidx, extra_cidxs in owner_mapping:
        primary_cust = customers[primary_cidx]
        skey = f"usr:shop:{uidx:02d}"
        sphone = f"0780100{uidx:04d}"
        su = _create_or_get_user(
            seed_key=skey,
            actor_manager=manager,
            phone=sphone,
            password=DEMO_PASSWORDS["shop"],
            name=primary_cust.owner_name or primary_cust.name,
            role=Role.SHOP,
            active=True,
            profile_id="prof_vip_shop" if uidx <= 6 else None,
            last_login_days_ago=None if uidx in (35, 36, 41) else (31 if uidx in (37, 38) else uidx % 7),
        )
        ShopMembership.objects.get_or_create(
            user=su,
            customer=primary_cust,
            defaults={"is_primary": True, "is_active_shop": True},
        )
        for ex_cidx in extra_cidxs:
            ShopMembership.objects.get_or_create(
                user=su,
                customer=customers[ex_cidx],
                defaults={"is_primary": False, "is_active_shop": False},
            )
        shop_users.append(su)
        all_users.append(su)

    # Negotiated SpecialPrices for top 8 VIP customers on top-10 anchor items
    for vip_cust in customers[:8]:
        for item in items[:10]:
            base_tier_price = item.prices.order_by("id").first()
            if not base_tier_price:
                continue
            sp_carton = max(250, int(base_tier_price.carton_price * Decimal("0.91")) // 250 * 250)
            sp_piece = max(250, int(base_tier_price.piece_price * Decimal("0.91")) // 50 * 50)
            SpecialPrice.objects.update_or_create(
                customer=vip_cust,
                item=item,
                defaults={"carton_price": sp_carton, "piece_price": sp_piece, "updated_by": manager},
            )

    # Generate gitignored DEMO_ACCOUNTS.local.md
    _write_demo_accounts_doc(
        manager=manager,
        accountants=accountants,
        keepers=keepers,
        staff_users=staff_users,
        sales_reps=sales_reps,
        delivery_reps=delivery_reps,
        shop_users=shop_users,
        all_users=all_users,
    )

    return {
        "all_users": all_users,
        "accountants": accountants,
        "keepers": keepers,
        "staff_users": staff_users,
        "sales_reps": sales_reps,
        "delivery_reps": delivery_reps,
        "shop_users": shop_users,
        "customers": customers,
        "active_customers": [c for c in customers if c.active and c.status == CustomerStatus.ACTIVE],
        "vehicles": vehicles,
    }


def _write_demo_accounts_doc(
    *,
    manager: User,
    accountants: list[User],
    keepers: list[User],
    staff_users: list[User],
    sales_reps: list[User],
    delivery_reps: list[User],
    shop_users: list[User],
    all_users: list[User],
) -> Path:
    out_dir = Path(settings.BASE_DIR) / "seed-output"
    out_dir.mkdir(parents=True, exist_ok=True)
    doc_path = out_dir / "DEMO_ACCOUNTS.local.md"
    lines = [
        "# Al-Nahrain Wholesale ERP — Local Demo Accounts (DO NOT COMMIT)",
        "",
        f"Generated locally inside gitignored `seed-output/` for development & demo testing (`Total users seeded: {len(all_users)}`).",
        "",
        "| Role | Name | Phone | Password | Status | Notes |",
        "| --- | --- | --- | --- | --- | --- |",
        f"| `manager` | {manager.name} | `{manager.phone}` | `{DEMO_PASSWORDS['manager']}` | active | System Owner / General Manager |",
    ]
    for a in accountants:
        lines.append(f"| `accountant` | {a.name} | `{a.phone}` | `{DEMO_PASSWORDS['accountant']}` | active | profile=`{a.profile_id or 'default'}` |")
    for k in keepers:
        wh_label = k.warehouse.name if k.warehouse_id and hasattr(k, "warehouse") and k.warehouse else f"wh#{k.warehouse_id}"
        lines.append(f"| `warehouse` | {k.name} | `{k.phone}` | `{DEMO_PASSWORDS['warehouse']}` | {'active' if k.active else 'disabled'} | {wh_label} (`{k.profile_id or 'default'}`) |")
    for st in [staff_users[0], staff_users[5], staff_users[8]]:
        wh_label = st.warehouse.name if st.warehouse_id and hasattr(st, "warehouse") and st.warehouse else f"wh#{st.warehouse_id}"
        lines.append(f"| `staff` | {st.name} | `{st.phone}` | `{DEMO_PASSWORDS['staff']}` | {'active' if st.active else 'disabled'} | {wh_label} (`{st.profile_id or 'default'}`) |")
    for sr in sales_reps:
        lines.append(f"| `rep (sales)` | {sr.name} | `{sr.phone}` | `{DEMO_PASSWORDS['rep']}` | active | target=`{sr.sales_target or 0:,}` IQD |")
    for dr in delivery_reps:
        lines.append(f"| `rep (delivery)` | {dr.name} | `{dr.phone}` | `{DEMO_PASSWORDS['rep']}` | active | fleet delivery |")
    for su in shop_users[:6]:
        ms_count = su.shop_memberships.count()
        lines.append(f"| `shop` | {su.name} | `{su.phone}` | `{DEMO_PASSWORDS['shop']}` | active | shops linked=`{ms_count}` |")
    lines.extend(
        [
            "",
            "## Additional Special Accounts",
            f"- Disabled staff user: `{staff_users[-1].phone}` (`{DEMO_PASSWORDS['staff']}`) — tests disabled login rejection.",
            "- Disabled sales rep: `07701000056` — tests deactivated rep.",
            "- Dual sales+delivery rep: `07701000055`.",
            "- Pending approval shop owner: `07801000041` (`customer` status=`pending`).",
            "",
        ]
    )
    doc_path.write_text("\n".join(lines), encoding="utf-8")
    return doc_path
