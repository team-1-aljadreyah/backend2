"""Company configuration, Iraqi warehouse network, price tiers, USD rates, categories, and 96 wholesale items."""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any

from apps.accounts.models import PriceTier, SystemSetting, User
from apps.accounts.profiles import save_site_config
from apps.catalog.models import Category, Item, ItemPrice, PriceChange
from apps.catalog.services import create_item
from apps.common.demo_media import bytes_to_data_url, render_company_logo
from apps.common.demo_seed.registry import frozen_now, get_seeded, register_seed
from apps.common.utils import baghdad_now, baghdad_today
from apps.exchange_rates.models import UsdRate
from apps.warehouses.models import Warehouse
from apps.warehouses.services import create_warehouse, save_warehouse_layout

PRICE_TIERS: list[dict[str, Any]] = [
    {"key": "tier:wholesale", "name": "جملة قياسية", "name_en": "Standard Wholesale", "sort_order": 1, "factor": Decimal("1.00")},
    {"key": "tier:vip", "name": "جملة كبرى (VIP)", "name_en": "Key Wholesale VIP", "sort_order": 2, "factor": Decimal("0.94")},
    {"key": "tier:malls", "name": "سوبرماركت ومولات", "name_en": "Supermarkets & Malls", "sort_order": 3, "factor": Decimal("0.97")},
    {"key": "tier:districts", "name": "موزعي الأقضية", "name_en": "District Distributors", "sort_order": 4, "factor": Decimal("0.96")},
]

CATEGORIES: list[tuple[str, str, int]] = [
    ("زيوت ودهون", "Oils & Fats", 1),
    ("حبوب وأرز", "Grains & Rice", 2),
    ("ألبان وأجبان", "Dairy & Cheese", 3),
    ("معلبات ومعاجين", "Canned & Pastes", 4),
    ("مشروبات وعصائر", "Beverages & Juices", 5),
    ("شاي وقهوة", "Tea & Coffee", 6),
    ("منظفات منزلية", "Home Cleaning", 7),
    ("عناية شخصية", "Personal Care", 8),
    ("مقرمشات وحلويات", "Snacks & Sweets", 9),
    ("بهارات وتوابل", "Spices & Condiments", 10),
]

WAREHOUSES: list[dict[str, Any]] = [
    {
        "key": "wh:kbl_main",
        "code": "WH-KBL-MAIN",
        "name": "المستودع المركزي - كربلاء",
        "name_en": "Karbala Central Hub",
        "address": "كربلاء المقدسة — الحي الصناعي، قرب مجسر العباس (ساعات العمل 07:30-17:30 | 32.6085, 44.0312)",
        "zones": [
            {"code": "A1", "name": "رفوف الحبوب والأرز"},
            {"code": "B1", "name": "ممر الزيوت والمعلبات"},
            {"code": "C1", "name": "قسم المنظفات والعناية"},
            {"code": "D1", "name": "المشروبات والحلويات"},
        ],
    },
    {
        "key": "wh:kbl_cold",
        "code": "WH-KBL-COLD",
        "name": "مستودع النهرين المبرد - كربلاء",
        "name_en": "Karbala Cold Storage",
        "address": "كربلاء المقدسة — طريق الحر السريع، مجمع النهرين المبرد (32.6340, 43.9985)",
        "zones": [
            {"code": "C1", "name": "غرفة التبريد (ألبان وأجبان)"},
            {"code": "C2", "name": "منطقة المشروبات المبردة"},
            {"code": "S1", "name": "الفرز والتحضير السريع"},
        ],
    },
    {
        "key": "wh:njf_depot",
        "code": "WH-NJF-01",
        "name": "فرع ومستودع النجف الأشرف",
        "name_en": "Najaf Regional Depot",
        "address": "النجف الأشرف — حي الحرفيين، الشارع الرئيسي (32.0258, 44.3462)",
        "zones": [
            {"code": "N1", "name": "الجناح الغذائي الشمالي"},
            {"code": "N2", "name": "قسم المنظفات والبهارات"},
            {"code": "STG", "name": "رصيف التحميل"},
        ],
    },
    {
        "key": "wh:bgd_karkh",
        "code": "WH-BGD-01",
        "name": "فرع ومستودع بغداد / الكرخ",
        "name_en": "Baghdad Karkh Hub",
        "address": "بغداد — الكرخ، المنطقة الصناعية / عويريج اللوجستي (33.2491, 44.3518)",
        "zones": [
            {"code": "K1", "name": "خط المواد الأساسية السريع"},
            {"code": "K2", "name": "قسم المشروبات والضيافة"},
            {"code": "K3", "name": "قسم التنظيف التجاري"},
        ],
    },
    {
        "key": "wh:bab_hillah",
        "code": "WH-BAB-01",
        "name": "فرع ومستودع الحلة / بابل",
        "name_en": "Hillah Babil Depot",
        "address": "بابل / الحلة — حي النسيج الصناعي، قرب تقاطع الجامعة (32.4735, 44.4321)",
        "zones": [
            {"code": "H1", "name": "مخزون بابل العام"},
            {"code": "H2", "name": "التسليم الفوري"},
        ],
    },
]

# 96 realistic wholesale synthetic items across all 10 categories:
# (ar_name, en_name, brand, origin, category_idx, pieces_per_carton, base_piece_price_iqd, purchase_cost_piece_iqd, min_stock_pieces, weight_kg)
ITEM_CATALOG: list[tuple[str, str, str, str, int, int, int, int, int, str]] = [
    # 0: زيوت ودهون (10)
    ("زيت دوار الشمس النهرين 1 لتر", "Al-Nahrain Sunflower Oil 1L", "النهرين الذهبية", "العراق", 0, 12, 2250, 1750, 36, "0.92"),
    ("زيت دوار الشمس النهرين 5 لتر", "Al-Nahrain Sunflower Oil 5L", "النهرين الذهبية", "العراق", 0, 4, 10500, 8250, 24, "4.60"),
    ("زيت ذرة بابل الصافي 1.8 لتر", "Babil Pure Corn Oil 1.8L", "بابل فودز", "تركيا", 0, 6, 4500, 3550, 24, "1.65"),
    ("سمن نباتي الفرات الذهبي 2 كغم", "Al-Furat Vegetable Ghee 2KG", "الفرات", "الإمارات", 0, 6, 5250, 4150, 18, "2.00"),
    ("زيت زيتون دجلة البكر 500 مل", "Dijlah Virgin Olive Oil 500ml", "دجلة", "الأردن", 0, 12, 6500, 5100, 24, "0.52"),
    ("زيت قلي تجاري السنبلة 18 لتر", "Al-Sunbula Commercial Frying Oil 18L", "السنبلة", "العراق", 0, 1, 34500, 27500, 12, "16.50"),
    ("زيت كانولا الغدير 900 مل", "Al-Ghadeer Canola Oil 900ml", "الغدير", "تركيا", 0, 12, 2500, 1950, 24, "0.85"),
    ("سمن حيواني روابي كربلاء 900 غم", "Rawabi Karbala Pure Ghee 900g", "روابي كربلاء", "العراق", 0, 6, 11500, 9100, 12, "0.95"),
    ("زيت سمسم الأصيل 250 مل", "Al-Aseel Sesame Oil 250ml", "الأصيل", "سوريا", 0, 12, 3750, 2900, 18, "0.30"),
    ("زبدة مائدة النخيل 200 غم", "Al-Nakheel Table Butter 200g", "النخيل", "السعودية", 0, 24, 1750, 1350, 36, "0.22"),
    # 1: حبوب وأرز (10)
    ("أرز بسمتي النهرين الذهبي 5 كغم", "Al-Nahrain Basmati Rice 5KG", "النهرين الذهبية", "الهند", 1, 6, 10500, 8400, 30, "5.00"),
    ("أرز بسمتي النهرين الذهبي 10 كغم", "Al-Nahrain Basmati Rice 10KG", "النهرين الذهبية", "الهند", 1, 3, 20000, 16200, 18, "10.00"),
    ("أرز عنبر الفرات الفاخر 5 كغم", "Al-Furat Anbar Rice 5KG", "الفرات", "العراق", 1, 6, 13500, 10900, 24, "5.00"),
    ("عدس أحمر مجروش الغدير 1 كغم", "Al-Ghadeer Red Lentils 1KG", "الغدير", "تركيا", 1, 12, 2000, 1520, 36, "1.00"),
    ("حمص حب ممتاز بابل 1 كغم", "Babil Whole Chickpeas 1KG", "بابل فودز", "العراق", 1, 12, 2250, 1720, 36, "1.00"),
    ("فاصوليا بيضاء دجلة 1 كغم", "Dijlah White Beans 1KG", "دجلة", "الأرجنتين", 1, 12, 2750, 2150, 24, "1.00"),
    ("طحين صفر النهرين المخبوزات 25 كغم", "Al-Nahrain Baker Flour 25KG", "النهرين الذهبية", "العراق", 1, 1, 21500, 17250, 15, "25.00"),
    ("برغل خشن كربلاء 900 غم", "Karbala Coarse Bulgur 900g", "روابي كربلاء", "تركيا", 1, 12, 1750, 1320, 24, "0.90"),
    ("معكرونة إسباغيتي بابل 400 غم", "Babil Spaghetti Pasta 400g", "بابل فودز", "تركيا", 1, 20, 1000, 740, 40, "0.42"),
    ("شوفان كامل الغدير 500 غم", "Al-Ghadeer Whole Oats 500g", "الغدير", "الإمارات", 1, 12, 1750, 1340, 24, "0.52"),
    # 2: ألبان وأجبان (10)
    ("حليب طويل الأمد النهرين 1 لتر", "Al-Nahrain UHT Milk 1L", "النهرين الذهبية", "السعودية", 2, 12, 1750, 1360, 48, "1.05"),
    ("حليب مجفف روابي 900 غم", "Rawabi Milk Powder 900g", "روابي كربلاء", "نيوزيلندا", 2, 12, 7500, 5950, 24, "0.98"),
    ("جبن مثلثات الفرات 120 غم", "Al-Furat Cheese Triangles 120g", "الفرات", "مصر", 2, 24, 1000, 760, 48, "0.14"),
    ("جبن شيدر قالب بابل 400 غم", "Babil Cheddar Block 400g", "بابل فودز", "هولندا", 2, 12, 3750, 2950, 24, "0.42"),
    ("لبن عيران دجلة 250 مل", "Dijlah Ayran Drink 250ml", "دجلة", "العراق", 2, 24, 500, 350, 48, "0.26"),
    ("قشطة المائدة الغدير 170 غم", "Al-Ghadeer Table Cream 170g", "الغدير", "السعودية", 2, 24, 1000, 780, 48, "0.19"),
    ("جبن موزاريلا مبشور السنبلة 1 كغم", "Al-Sunbula Shredded Mozzarella 1KG", "السنبلة", "الدنمارك", 2, 6, 8500, 6750, 18, "1.02"),
    ("لبنة تركية فاخرة كربلاء 500 غم", "Karbala Turkish Labneh 500g", "روابي كربلاء", "تركيا", 2, 12, 2750, 2150, 24, "0.53"),
    ("حليب مكثف محلى النخيل 397 غم", "Al-Nakheel Condensed Milk 397g", "النخيل", "ماليزيا", 2, 24, 1500, 1140, 24, "0.42"),
    ("جبن كريمي قابل للدهن دجلة 240 غم", "Dijlah Cream Cheese Jar 240g", "دجلة", "الأردن", 2, 12, 2250, 1740, 24, "0.35"),
    # 3: معلبات ومعاجين (10)
    ("معجون طماطم النهرين 800 غم", "Al-Nahrain Tomato Paste 800g", "النهرين الذهبية", "العراق", 3, 12, 2000, 1540, 48, "0.86"),
    ("معجون طماطم النهرين 400 غم", "Al-Nahrain Tomato Paste 400g", "النهرين الذهبية", "العراق", 3, 24, 1250, 940, 48, "0.45"),
    ("تونة بيضاء قطع دجلة 185 غم", "Dijlah Chunk White Tuna 185g", "دجلة", "تايلاند", 3, 24, 1750, 1340, 48, "0.21"),
    ("فول مدمس بالخلطة الغدير 400 غم", "Al-Ghadeer Foul Medames 400g", "الغدير", "الأردن", 3, 24, 1000, 740, 48, "0.43"),
    ("ذرة حلوة بابل 340 غم", "Babil Sweet Corn Can 340g", "بابل فودز", "إسبانيا", 3, 24, 1250, 950, 36, "0.38"),
    ("فطر مقطع معلب الفرات 400 غم", "Al-Furat Sliced Mushrooms 400g", "الفرات", "هولندا", 3, 24, 1500, 1150, 36, "0.42"),
    ("حمص بطحينة جاهز الأصيل 380 غم", "Al-Aseel Hummus Tahini 380g", "الأصيل", "الأردن", 3, 24, 1250, 960, 24, "0.41"),
    ("مربى مشمش طبيعي روابي 450 غم", "Rawabi Apricot Jam 450g", "روابي كربلاء", "العراق", 3, 12, 2000, 1520, 24, "0.60"),
    ("دبس تمر كربلاء الفاخر 850 غم", "Karbala Premium Date Syrup 850g", "روابي كربلاء", "العراق", 3, 12, 2750, 2120, 24, "0.95"),
    ("زيتون أسود منزوع النوى النخيل 350 غم", "Al-Nakheel Pitted Black Olives 350g", "النخيل", "تركيا", 3, 12, 2250, 1720, 24, "0.48"),
    # 4: مشروبات وعصائر (10)
    ("مياه شرب النهرين 500 مل", "Al-Nahrain Bottled Water 500ml", "النهرين الذهبية", "العراق", 4, 24, 250, 160, 120, "0.52"),
    ("مياه شرب النهرين 1.5 لتر", "Al-Nahrain Bottled Water 1.5L", "النهرين الذهبية", "العراق", 4, 6, 500, 340, 60, "1.55"),
    ("عصير مانجو الفرات 1 لتر", "Al-Furat Mango Nectar 1L", "الفرات", "العراق", 4, 12, 1250, 940, 36, "1.06"),
    ("عصير برتقال طبيعي بابل 250 مل", "Babil Orange Juice 250ml", "بابل فودز", "الإمارات", 4, 24, 500, 360, 48, "0.27"),
    ("عصير تفاح الغدير 1 لتر", "Al-Ghadeer Apple Juice 1L", "الغدير", "تركيا", 4, 12, 1250, 960, 36, "1.06"),
    ("شراب شعير بالليمون دجلة 330 مل", "Dijlah Lemon Malt Drink 330ml", "دجلة", "الأردن", 4, 24, 750, 560, 48, "0.35"),
    ("مشروب غازي كولا بابل 330 مل", "Babil Cola Can 330ml", "بابل فودز", "العراق", 4, 24, 500, 360, 72, "0.35"),
    ("شراب رمان مركز كربلاء 700 مل", "Karbala Pomegranate Concentrate 700ml", "روابي كربلاء", "العراق", 4, 12, 3250, 2520, 24, "0.88"),
    ("عصير جوافة روابي 200 مل", "Rawabi Guava Tetra 200ml", "روابي كربلاء", "مصر", 4, 24, 500, 340, 48, "0.22"),
    ("مياه فوارة النخيل 300 مل", "Al-Nakheel Sparkling Water 300ml", "النخيل", "تركيا", 4, 24, 750, 540, 24, "0.34"),
    # 5: شاي وقهوة (9)
    ("شاي سيلاني النهرين الفاخر 500 غم", "Al-Nahrain Ceylon Tea 500g", "النهرين الذهبية", "سريلانكا", 5, 12, 4750, 3720, 36, "0.55"),
    ("شاي سيلاني النهرين 1 كغم", "Al-Nahrain Ceylon Tea 1KG", "النهرين الذهبية", "سريلانكا", 5, 6, 9250, 7350, 24, "1.08"),
    ("أكياس شاي الفرات 100 كيس", "Al-Furat Tea Bags 100ct", "الفرات", "سريلانكا", 5, 12, 3250, 2520, 24, "0.28"),
    ("قهوة سريعة الذوبان دجلة 200 غم", "Dijlah Instant Coffee 200g", "دجلة", "البرازيل", 5, 12, 6500, 5100, 24, "0.42"),
    ("قهوة عربية بالهيل الغدير 250 غم", "Al-Ghadeer Cardamom Coffee 250g", "الغدير", "الأردن", 5, 12, 4250, 3300, 24, "0.28"),
    ("قهوة تركية محمصة الأصيل 250 غم", "Al-Aseel Turkish Coffee 250g", "الأصيل", "تركيا", 5, 24, 3000, 2340, 24, "0.26"),
    ("كابتشينو 3 في 1 بابل 20 ظرف", "Babil 3in1 Coffee Sachets 20pk", "بابل فودز", "ماليزيا", 5, 12, 3500, 2720, 24, "0.40"),
    ("شاي أخضر بالنعناع روابي 25 كيس", "Rawabi Mint Green Tea 25ct", "روابي كربلاء", "الإمارات", 5, 24, 1500, 1120, 24, "0.10"),
    ("مبيض قهوة النخيل 400 غم", "Al-Nakheel Coffee Creamer 400g", "النخيل", "تايلاند", 5, 12, 2750, 2120, 24, "0.48"),
    # 6: منظفات منزلية (10)
    ("مسحوق غسيل أوتوماتيك النهرين 3 كغم", "Al-Nahrain Automatic Detergent 3KG", "النهرين للنظافة", "العراق", 6, 4, 5750, 4450, 24, "3.10"),
    ("سائل جلي ليمون الفرات 1 لتر", "Al-Furat Lemon Dish Liquid 1L", "الفرات", "العراق", 6, 12, 1500, 1120, 48, "1.06"),
    ("مبيض ومعقم كلور دجلة 3.7 لتر", "Dijlah Bleach Disinfectant 3.7L", "دجلة", "العراق", 6, 4, 2750, 2080, 24, "3.85"),
    ("منعم أقمشة زهور الغدير 2 لتر", "Al-Ghadeer Fabric Softener 2L", "الغدير", "تركيا", 6, 6, 3500, 2720, 18, "2.10"),
    ("منظف أرضيات صنوبر بابل 1.5 لتر", "Babil Pine Floor Cleaner 1.5L", "بابل فودز", "الأردن", 6, 12, 2000, 1520, 24, "1.58"),
    ("مناديل وجه ناعمة النهرين 5 علب", "Al-Nahrain Facial Tissues 5pk", "النهرين للنظافة", "العراق", 6, 8, 3750, 2900, 32, "1.10"),
    ("ورق مطبخ ماص روابي 4 رولات", "Rawabi Kitchen Towels 4pk", "روابي كربلاء", "العراق", 6, 10, 2750, 2120, 30, "0.85"),
    ("أكياس نفايات متينة السنبلة 30 كيس", "Al-Sunbula Heavy Trash Bags 30ct", "السنبلة", "السعودية", 6, 20, 1250, 920, 40, "0.45"),
    ("بخاخ منظف زجاج النخيل 750 مل", "Al-Nakheel Glass Cleaner 750ml", "النخيل", "تركيا", 6, 12, 1750, 1340, 24, "0.82"),
    ("أقراص غسالة صحون دجلة 30 قرص", "Dijlah Dishwasher Tabs 30ct", "دجلة", "ألمانيا", 6, 8, 8500, 6700, 16, "0.62"),
    # 7: عناية شخصية (9)
    ("شامبو العناية اليومية الغدير 400 مل", "Al-Ghadeer Daily Shampoo 400ml", "الغدير", "تركيا", 7, 12, 3250, 2500, 24, "0.45"),
    ("صابون أيدي سائل بالعود دجلة 500 مل", "Dijlah Oud Hand Wash 500ml", "دجلة", "الإمارات", 7, 12, 2250, 1720, 24, "0.55"),
    ("معجون أسنان حماية متكاملة بابل 100 مل", "Babil Total Toothpaste 100ml", "بابل فودز", "ألمانيا", 7, 24, 1750, 1320, 36, "0.14"),
    ("حفاضات أطفال مقاس 4 النهرين 44 قطعة", "Al-Nahrain Baby Diapers Sz4 44ct", "النهرين للنظافة", "تركيا", 7, 4, 8750, 6900, 24, "1.65"),
    ("مناديل مبللة للأطفال روابي 72 منديل", "Rawabi Baby Wipes 72ct", "روابي كربلاء", "تركيا", 7, 24, 1250, 940, 36, "0.40"),
    ("صابون قطع استحمام الأصيل 4 قطع", "Al-Aseel Bath Soap Bar 4pk", "الأصيل", "الأردن", 7, 18, 2250, 1740, 24, "0.50"),
    ("مزيل عرق رول أون الفرات 50 مل", "Al-Furat Roll-on Deodorant 50ml", "الفرات", "فرنسا", 7, 12, 3000, 2320, 24, "0.12"),
    ("جل استحمام منعش النخيل 500 مل", "Al-Nakheel Fresh Shower Gel 500ml", "النخيل", "تركيا", 7, 12, 3250, 2520, 24, "0.56"),
    ("كريم مرطب للجلد دجلة 150 مل", "Dijlah Skin Moisturizer 150ml", "دجلة", "بولندا", 7, 12, 2750, 2120, 24, "0.18"),
    # 8: مقرمشات وحلويات (9)
    ("بسكويت شاي سادة النهرين 24 قطعة", "Al-Nahrain Tea Biscuit Box 24pk", "النهرين الذهبية", "العراق", 8, 6, 3500, 2700, 30, "1.20"),
    ("ويفر بالشوكولاتة الفرات 12 قطعة", "Al-Furat Chocolate Wafer 12pk", "الفرات", "تركيا", 8, 12, 2250, 1720, 36, "0.48"),
    ("شيبس بطاطا بالملح بابل 20 كيس", "Babil Salted Chips Carton 20pk", "بابل فودز", "العراق", 8, 6, 3750, 2880, 30, "0.90"),
    ("شوكولاتة داكنة بالحليب الغدير 100 غم", "Al-Ghadeer Milk Chocolate 100g", "الغدير", "تركيا", 8, 24, 1250, 940, 36, "0.11"),
    ("معمول بالتمر العراقي روابي 16 قطعة", "Rawabi Date Maamoul 16pk", "روابي كربلاء", "العراق", 8, 12, 2500, 1920, 30, "0.60"),
    ("كيك محشي بالكاكاو دجلة 24 قطعة", "Dijlah Cocoa Filled Cake 24pk", "دجلة", "مصر", 8, 6, 4000, 3100, 24, "1.05"),
    ("علكة بالنعناع المنعش النخيل علبة 20", "Al-Nakheel Mint Gum 20pk", "النخيل", "تركيا", 8, 12, 3000, 2320, 24, "0.32"),
    ("فستق محمص مملح السنبلة 250 غم", "Al-Sunbula Roasted Pistachios 250g", "السنبلة", "إيران", 8, 12, 5500, 4350, 20, "0.27"),
    ("حلاوة طحينية كربلاء 500 غم", "Karbala Sesame Halawa 500g", "روابي كربلاء", "العراق", 8, 12, 3250, 2520, 24, "0.54"),
    # 9: بهارات وتوابل (9)
    ("فلفل أسود مطحون النهرين 200 غم", "Al-Nahrain Ground Black Pepper 200g", "النهرين الذهبية", "فيتنام", 9, 12, 3000, 2320, 24, "0.24"),
    ("بهارات برياني عراقية روابي 200 غم", "Rawabi Iraqi Biryani Spice 200g", "روابي كربلاء", "العراق", 9, 12, 2250, 1720, 24, "0.24"),
    ("كمون مطحون ممتاز الفرات 250 غم", "Al-Furat Ground Cumin 250g", "الفرات", "سوريا", 9, 12, 2500, 1940, 24, "0.28"),
    ("كركم أصلي بابل 250 غم", "Babil Pure Turmeric 250g", "بابل فودز", "الهند", 9, 12, 2000, 1520, 24, "0.28"),
    ("ملح طعام يودي دجلة 750 غم", "Dijlah Iodized Table Salt 750g", "دجلة", "العراق", 9, 20, 500, 340, 60, "0.76"),
    ("ماجي مرقة دجاج الغدير 24 مكعب", "Al-Ghadeer Chicken Bouillon 24ct", "الغدير", "مصر", 9, 24, 1500, 1140, 36, "0.26"),
    ("سماق عراقي أحمر كربلاء 200 غم", "Karbala Red Sumac 200g", "روابي كربلاء", "العراق", 9, 12, 2000, 1540, 24, "0.23"),
    ("خل عنب طبيعي الأصيل 1 لتر", "Al-Aseel Grape Vinegar 1L", "الأصيل", "العراق", 9, 12, 1250, 920, 24, "1.05"),
    ("صلصة رمان حامض النخيل 500 مل", "Al-Nakheel Pomegranate Molasses 500ml", "النخيل", "لبنان", 9, 12, 2750, 2140, 24, "0.62"),
]


def _ean13(prefix11: str, index: int) -> str:
    body12 = f"{prefix11[:9]}{index:03d}"[:12]
    total = sum(int(ch) * (1 if i % 2 == 0 else 3) for i, ch in enumerate(body12))
    check = (10 - (total % 10)) % 10
    return f"{body12}{check}"


def _build_site_payload(logo_data_url: str | None) -> dict[str, Any]:
    return {
        "brand": {
            "name": "النهرين",
            "name_en": "Al-Nahrain",
            "tagline": "توزيع المواد الغذائية والاستهلاكية بالجملة — العراق",
            "tagline_en": "Wholesale Food & FMCG Distribution — Iraq",
            "company": "شركة النهرين الذهبية للتجارة العامة والتوزيع المحدودة",
            "company_en": "Al-Nahrain Golden Trading & Wholesale Distribution Co.",
            "phone": "07701000000",
            **({"logo": logo_data_url} if logo_data_url else {}),
        },
        "nav": {
            "order": [],
            "hidden": [],
            "groups": {},
            "labels": {},
            "group_labels": {},
        },
        "dashboard": {
            "order": [],
            "hidden": [],
            "labels": {},
        },
        "categories": [{"name": ar, "name_en": en} for ar, en, _ in CATEGORIES],
        "texts": {
            "working_hours": "السبت – الخميس | 08:00 ص – 05:00 م",
            "working_hours_en": "Sat – Thu | 08:00 – 17:00 (Asia/Baghdad)",
            "hq_city": "كربلاء المقدسة — الحي الصناعي",
            "tax_notice": "جميع الأسعار بالدينار العراقي (IQD)",
        },
        "layouts": {},
        "look": {
            "action": "#0f766e",
        },
        "apps": [
            {
                "id": "app_wh_ops",
                "name": "عمليات المستودعات",
                "name_en": "Warehouse Ops",
                "color": "#0f766e",
                "pages": ["/inventory", "/receipts", "/transfers", "/stocktakes", "/deliveries"],
            },
            {
                "id": "app_fin_desk",
                "name": "مكتب المحاسبة والتحصيل",
                "name_en": "Finance & Collections",
                "color": "#1d4ed8",
                "pages": ["/invoices", "/payments", "/expenses", "/returns", "/commissions", "/reports"],
            },
        ],
        "profiles": [
            {
                "id": "prof_wh_keeper",
                "name": "أمين مستودع قياسي",
                "name_en": "Standard Warehouse Keeper",
                "role": "warehouse",
                "denied": ["/reports", "/expenses", "/payments"],
                "default_app": "app_wh_ops",
            },
            {
                "id": "prof_wh_picker",
                "name": "موظف تجهيز وتحميل",
                "name_en": "Picker & Loader",
                "role": "staff",
                "denied": ["/invoices", "/payments", "/expenses", "/reports"],
                "default_app": "app_wh_ops",
            },
            {
                "id": "prof_sales_rep",
                "name": "مندوب مبيعات ميداني",
                "name_en": "Field Sales Rep",
                "role": "rep",
                "denied": ["/stocktakes", "/supply", "/expenses"],
                "can": {"orders": True, "payments": True, "returns": False},
            },
            {
                "id": "prof_dlv_rep",
                "name": "مندوب توصيل أسطول",
                "name_en": "Fleet Delivery Driver",
                "role": "rep",
                "denied": ["/items", "/stocktakes", "/supply", "/reports"],
                "can": {"orders": False, "payments": True, "returns": True},
            },
            {
                "id": "prof_senior_acc",
                "name": "محاسب مالي أول",
                "name_en": "Senior Financial Accountant",
                "role": "accountant",
                "denied": [],
                "default_app": "app_fin_desk",
            },
            {
                "id": "prof_vip_shop",
                "name": "مشتري متاجر VIP",
                "name_en": "VIP Shop Buyer",
                "role": "shop",
                "denied": [],
            },
        ],
        "fields": [
            {
                "id": "cf_item_sku",
                "object": "item",
                "label": "رمز الصنف SKU",
                "label_en": "Item SKU",
                "type": "text",
                "required": False,
            },
            {
                "id": "cf_item_brand",
                "object": "item",
                "label": "العلامة التجارية",
                "label_en": "Brand",
                "type": "text",
                "required": False,
            },
            {
                "id": "cf_item_origin",
                "object": "item",
                "label": "بلد المنشأ",
                "label_en": "Country of Origin",
                "type": "text",
                "required": False,
            },
            {
                "id": "cf_cust_type",
                "object": "customer",
                "label": "تصنيف المتجر",
                "label_en": "Shop Classification",
                "type": "list",
                "options": ["هايبر ماركت", "سوبرماركت", "أسواق جملة", "موزع محلي", "تموين مطاعم"],
                "required": False,
            },
            {
                "id": "cf_cust_gov",
                "object": "customer",
                "label": "المحافظة",
                "label_en": "Governorate",
                "type": "list",
                "options": ["كربلاء المقدسة", "النجف الأشرف", "بغداد", "بابل", "الديوانية"],
                "required": False,
            },
            {
                "id": "cf_cust_contract",
                "object": "customer",
                "label": "تاريخ التعاقد",
                "label_en": "Contract Date",
                "type": "date",
                "required": False,
            },
            {
                "id": "cf_order_channel",
                "object": "order",
                "label": "قناة الطلب",
                "label_en": "Order Channel",
                "type": "list",
                "options": ["زيارة ميدانية", "تطبيق المتجر", "اتصال هاتفي", "مزامنة أوفلاين"],
                "required": False,
            },
        ],
        "rules": [
            {
                "id": "rule_high_order_value",
                "name": "إشعار الطلبات الكبيرة فوق 2 مليون",
                "name_en": "Alert on Large Orders > 2M IQD",
                "trigger": "order_created",
                "conditions": [{"field": "total", "op": "gt", "value": 2000000}],
                "notify": ["manager", "accountant"],
                "approval": False,
            },
            {
                "id": "rule_high_discount_approval",
                "name": "موافقة الخصومات التجارية فوق 10%",
                "name_en": "Approval on Commercial Discount > 10%",
                "trigger": "order_created",
                "conditions": [{"field": "discount", "op": "gt", "value": 10}],
                "notify": ["manager"],
                "approval": True,
            },
            {
                "id": "rule_big_payment",
                "name": "إشعار استلام دفعة فوق 5 مليون",
                "name_en": "Alert on Collection > 5M IQD",
                "trigger": "payment_created",
                "conditions": [{"field": "amount", "op": "gte", "value": 5000000}],
                "notify": ["manager", "accountant"],
                "approval": False,
            },
            {
                "id": "rule_big_return",
                "name": "مراجعة المرتجعات فوق 500 ألف",
                "name_en": "Review Returns > 500K IQD",
                "trigger": "return_created",
                "conditions": [{"field": "amount", "op": "gt", "value": 500000}],
                "notify": ["manager", "warehouse"],
                "approval": False,
            },
            {
                "id": "rule_high_expense",
                "name": "تنبيه الصرفيات التشغيلية فوق 400 ألف",
                "name_en": "Alert Operating Expense > 400K IQD",
                "trigger": "expense_created",
                "conditions": [{"field": "amount", "op": "gt", "value": 400000}],
                "notify": ["manager", "accountant"],
                "approval": False,
            },
        ],
    }


def seed_catalog_foundation(manager: User, *, with_media: bool = False) -> dict[str, Any]:
    """Seed SiteConfig, SystemSettings, USD rates, 5 warehouses, 4 price tiers, 10 categories, and 96 items."""
    logo_meta: dict[str, Any] | None = None
    logo_data_url: str | None = None
    if with_media:
        logo_meta = render_company_logo("company:logo")
        logo_data_url = bytes_to_data_url(logo_meta["_bytes"], "image/webp")

    # Apply company settings idempotently
    save_site_config(actor=manager, data=_build_site_payload(logo_data_url))
    from apps.accounts.models import SiteConfig

    cfg = SiteConfig.get_solo()
    register_seed("site:config", cfg)
    if logo_meta and cfg.brand_logo_path:
        register_seed(
            "media:company:logo",
            cfg,
            extra={"bucket": "product-images", "key": cfg.brand_logo_path, "asset_id": logo_meta["asset_id"]},
        )

    # Commercial settings
    s = SystemSetting.get_solo()
    s.usd_rate = Decimal("1500.00")
    s.reservation_minutes = 90
    s.low_stock_threshold = 24
    s.commission_pct = Decimal("1.50")
    s.expiry_warn_days = 45
    s.save()
    register_seed("settings:system", s)

    # 40-day realistic USD/IQD rate curve ending today at 1500
    today = baghdad_today()
    curve = [1485, 1488, 1490, 1492, 1495, 1495, 1498, 1502, 1505, 1508, 1510, 1507, 1505, 1500]
    for offset in range(39, -1, -1):
        d = today - timedelta(days=offset)
        rate_val = Decimal(str(curve[(39 - offset) % len(curve)]))
        if offset == 0:
            rate_val = Decimal("1500.00")
        ur = UsdRate.objects.filter(date=d).first()
        if not ur:
            ur = UsdRate.objects.create(date=d, rate=rate_val, by=manager)
        elif ur.rate != rate_val:
            ur.rate = rate_val
            ur.save(update_fields=["rate"])
        register_seed(f"usdrate:{d.isoformat()}", ur)

    # 4 Price Tiers
    tiers: list[PriceTier] = []
    for spec in PRICE_TIERS:
        tier = get_seeded(spec["key"], PriceTier) or PriceTier.objects.filter(name=spec["name"]).first()
        if not tier:
            tier = PriceTier.objects.create(
                name=spec["name"],
                name_en=spec["name_en"],
                sort_order=spec["sort_order"],
            )
        register_seed(spec["key"], tier)
        tiers.append(tier)

    # 10 Categories
    categories: dict[str, Category] = {}
    for ar_name, en_name, order in CATEGORIES:
        ckey = f"cat:{order}"
        cat = get_seeded(ckey, Category) or Category.objects.filter(name=ar_name).first()
        if not cat:
            cat = Category.objects.create(name=ar_name, name_en=en_name, sort_order=order)
        else:
            cat.name_en = en_name
            cat.sort_order = order
            cat.save(update_fields=["name_en", "sort_order"])
        register_seed(ckey, cat)
        categories[ar_name] = cat

    # 5 Iraqi Warehouses
    warehouses: dict[str, Warehouse] = {}
    for spec in WAREHOUSES:
        wh = get_seeded(spec["key"], Warehouse) or Warehouse.objects.filter(code=spec["code"]).first()
        if not wh:
            res = create_warehouse(
                actor=manager,
                data={
                    "code": spec["code"],
                    "name": spec["name"],
                    "name_en": spec["name_en"],
                    "address": spec["address"],
                    "active": True,
                },
            )
            wh = Warehouse.objects.get(id=res["id"])
        register_seed(spec["key"], wh)
        warehouses[spec["key"]] = wh

    # 96 Synthetic Wholesale Items + Multi-tier Pricing + 3-Day PriceChange History
    now_dt = baghdad_now()
    items: list[Item] = []
    item_costs: dict[int, int] = {}
    tier_factor_map = {t.id: PRICE_TIERS[i]["factor"] for i, t in enumerate(tiers)}

    for idx, row in enumerate(ITEM_CATALOG, start=1):
        ar_name, en_name, brand, origin, cat_idx, ppc, base_pp, cost_pp, min_stk, w_kg = row
        cat_ar = CATEGORIES[cat_idx][0]
        barcode = _ean13("629700100", idx)
        ikey = f"item:{idx:03d}"
        sku_code = f"NAH-{cat_idx + 1:02d}{idx:03d}"

        item = get_seeded(ikey, Item) or Item.objects.filter(barcode=barcode).first()
        if not item:
            prices_payload = []
            for t in tiers:
                factor = tier_factor_map[t.id]
                # Round cleanly to nearest 25 IQD for realism while matching integer contract
                pp = int((Decimal(base_pp) * factor) // 50 * 50)
                cp = pp * ppc
                prices_payload.append({"tier_id": t.id, "carton_price": cp, "piece_price": pp})

            with frozen_now(now_dt - timedelta(days=92)):
                created = create_item(
                    actor=manager,
                    data={
                        "name": ar_name,
                        "name_en": en_name,
                        "barcode": barcode,
                        "category": cat_ar,
                        "pieces_per_carton": ppc,
                        "min_stock": min_stk,
                        "commission_percent": "1.50" if idx % 3 == 0 else ("2.00" if idx % 7 == 0 else "0.00"),
                        "weight_kg": w_kg,
                        "active": idx not in (94, 95),  # keep 2 realistic inactive discontinued SKUs
                        "prices": prices_payload,
                        "custom": {
                            "cf_item_sku": sku_code,
                            "cf_item_brand": brand,
                            "cf_item_origin": origin,
                        },
                    },
                )
            item = Item.objects.get(id=created["id"])

            # Add 2-day historical PriceChange on top-25 fast movers so offline sync testing works
            if idx <= 25:
                std_price = ItemPrice.objects.get(item=item, tier=tiers[0])
                old_pp = max(250, std_price.piece_price - 100)
                old_cp = old_pp * ppc
                PriceChange.objects.create(
                    item=item,
                    tier=tiers[0],
                    carton_price=old_cp,
                    piece_price=old_pp,
                    changed_by=manager,
                    effective_from=now_dt - timedelta(days=2),
                )
        register_seed(ikey, item, extra={"sku": sku_code, "cost_piece": cost_pp})
        items.append(item)
        item_costs[item.id] = cost_pp

    # Arrange realistic storage zones + product shelf placements per warehouse
    for spec in WAREHOUSES:
        wh = warehouses[spec["key"]]
        zones = spec["zones"]
        places = {
            str(it.id): zones[i % len(zones)]["code"]
            for i, it in enumerate(items)
        }
        save_warehouse_layout(actor=manager, warehouse_id=wh.id, data={"zones": zones, "places": places})

    return {
        "site_config": cfg,
        "logo_meta": logo_meta,
        "tiers": tiers,
        "categories": categories,
        "warehouses": warehouses,
        "warehouse_list": [warehouses[w["key"]] for w in WAREHOUSES],
        "items": items,
        "item_costs": item_costs,
    }
