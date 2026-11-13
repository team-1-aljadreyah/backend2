"""Deterministic studio WebP media renderer & manifest builder for realistic ERP demo seeding."""
from __future__ import annotations

import base64
import hashlib
import io
import json
from pathlib import Path
from typing import Any

from django.conf import settings
from PIL import Image, ImageDraw, ImageFont

SEED_ASSETS_DIR = Path(settings.BASE_DIR) / "seed_assets"

_PALETTES: list[tuple[tuple[int, int, int], tuple[int, int, int], tuple[int, int, int]]] = [
    ((240, 249, 246), (15, 118, 110), (217, 119, 6)),
    ((239, 246, 255), (29, 78, 216), (14, 165, 233)),
    ((254, 249, 235), (180, 83, 9), (22, 163, 74)),
    ((250, 245, 255), (109, 40, 217), (236, 72, 153)),
    ((240, 253, 244), (21, 128, 61), (202, 138, 4)),
    ((254, 242, 242), (185, 28, 28), (245, 158, 11)),
    ((241, 245, 249), (51, 65, 85), (13, 148, 136)),
    ((255, 247, 237), (194, 65, 12), (30, 64, 175)),
]

_SKIN_TONES = [
    (234, 192, 156),
    (216, 169, 128),
    (194, 141, 98),
    (242, 206, 176),
    (168, 116, 78),
]

_HAIR_COLORS = [
    (28, 25, 23),
    (55, 42, 35),
    (82, 58, 42),
    (71, 85, 105),
]


def _seed_hash(seed_key: str) -> int:
    return int(hashlib.sha256(seed_key.encode("utf-8")).hexdigest()[:12], 16)


def _save_webp_pair(
    img: Image.Image,
    rel_full: str,
    rel_thumb: str,
    thumb_size: tuple[int, int],
) -> tuple[bytes, str, int, int]:
    """Save full-size and thumbnail WebP files inside `seed_assets/` and return (full_bytes, sha256, w, h)."""
    full_path = SEED_ASSETS_DIR / rel_full
    thumb_path = SEED_ASSETS_DIR / rel_thumb
    full_path.parent.mkdir(parents=True, exist_ok=True)
    thumb_path.parent.mkdir(parents=True, exist_ok=True)

    rgb = img.convert("RGB")
    buf = io.BytesIO()
    rgb.save(buf, format="WEBP", quality=86, method=4)
    full_bytes = buf.getvalue()
    full_path.write_bytes(full_bytes)

    thumb = rgb.copy()
    thumb.thumbnail(thumb_size, Image.Resampling.LANCZOS)
    t_buf = io.BytesIO()
    thumb.save(t_buf, format="WEBP", quality=80, method=4)
    thumb_path.write_bytes(t_buf.getvalue())

    sha = hashlib.sha256(full_bytes).hexdigest()
    return full_bytes, sha, rgb.width, rgb.height


def bytes_to_data_url(raw: bytes, mime: str = "image/webp") -> str:
    """Encode image bytes as a data URL accepted by service endpoints."""
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


def _draw_soft_gradient(img: Image.Image, top: tuple[int, int, int], bottom: tuple[int, int, int]) -> None:
    draw = ImageDraw.Draw(img)
    w, h = img.size
    for y in range(h):
        t = y / max(1, h - 1)
        c = tuple(int(top[i] * (1 - t) + bottom[i] * t) for i in range(3))
        draw.line([(0, y), (w, y)], fill=c)


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for candidate in ("C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/segoeui.ttf"):
        if Path(candidate).is_file():
            try:
                return ImageFont.truetype(candidate, size=size)
            except OSError:
                pass
    return ImageFont.load_default()


def render_company_logo(seed_key: str = "company:logo") -> dict[str, Any]:
    """Build company logo WebP (using AI logo if copied into seed_assets/company/al_nahrain_logo_ai.{jpg,png})."""
    ai_candidate = next(
        (p for name in ("al_nahrain_logo_ai.jpg", "al_nahrain_logo_ai.png") if (p := SEED_ASSETS_DIR / "company" / name).is_file()),
        None,
    )
    if ai_candidate is not None:
        img = Image.open(ai_candidate).convert("RGB")
        img.thumbnail((512, 512), Image.Resampling.LANCZOS)
        source = "ai_generated"
    else:
        img = Image.new("RGB", (512, 512), (255, 255, 255))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle([36, 36, 476, 476], radius=96, fill=(15, 118, 110))
        d.rounded_rectangle([54, 54, 458, 458], radius=82, outline=(251, 191, 36), width=6)
        # Flowing two-river waves (Tigris & Euphrates motif)
        d.arc([95, 175, 415, 315], 190, 355, fill=(94, 234, 212), width=24)
        d.arc([95, 250, 415, 390], 190, 355, fill=(204, 251, 241), width=20)
        # Golden wheat / emblem crown
        for cx, cy in ((256, 132), (222, 165), (290, 165)):
            d.ellipse([cx - 18, cy - 18, cx + 18, cy + 18], fill=(245, 158, 11))
        source = "synthetic_pillow_studio"

    rel_full = "company/al_nahrain_logo.webp"
    rel_thumb = "company/thumbs/al_nahrain_logo_thumb.webp"
    raw, sha, w, h = _save_webp_pair(img, rel_full, rel_thumb, (160, 160))
    return {
        "asset_id": "asset-company-logo",
        "entity_type": "company",
        "seed_key": seed_key,
        "file_path": rel_full,
        "thumb_path": rel_thumb,
        "storage_bucket": "product-images",
        "storage_key": "brand/logo.webp",
        "format": "WEBP",
        "dimensions": {"width": w, "height": h},
        "alt_ar": "شعار شركة النهرين للمواد الغذائية بالجملة",
        "alt_en": "Al-Nahrain Wholesale Foods Logo",
        "source": source,
        "license": "synthetic_local_demo_asset",
        "sha256": sha,
        "_bytes": raw,
    }


def _packaging_silhouette(
    draw: ImageDraw.ImageDraw,
    style: str,
    primary: tuple[int, int, int],
    accent: tuple[int, int, int],
    rng_bits: int,
) -> None:
    """Draw high-contrast studio wholesale packaging shapes centered in a 640x640 canvas."""
    # Contact floor shadow
    draw.ellipse([160, 515, 480, 560], fill=(210, 218, 226))
    highlight = tuple(min(255, c + 55) for c in primary)
    label_bg = (255, 255, 255)

    if style == "bottle":
        # Cap + neck + tall curved bottle
        draw.rounded_rectangle([282, 84, 358, 128], radius=10, fill=accent)
        draw.polygon([(288, 128), (352, 128), (392, 200), (248, 200)], fill=primary)
        draw.rounded_rectangle([232, 192, 408, 530], radius=34, fill=primary, outline=(255, 255, 255), width=3)
        draw.rounded_rectangle([252, 212, 282, 504], radius=12, fill=highlight)
        draw.rounded_rectangle([244, 285, 396, 430], radius=14, fill=label_bg)
        draw.rectangle([244, 285, 396, 316], fill=accent)
    elif style == "sack":
        # 5KG/25KG stitched grain sack
        draw.polygon([(220, 125), (420, 125), (446, 525), (194, 525)], fill=(246, 240, 224), outline=primary, width=6)
        draw.rounded_rectangle([206, 108, 434, 146], radius=12, fill=primary)
        draw.rounded_rectangle([226, 220, 414, 420], radius=24, fill=primary)
        draw.ellipse([274, 252, 366, 344], fill=accent)
    elif style == "carton":
        # Tetra / wholesale carton box with isometric top & side
        draw.rounded_rectangle([205, 165, 435, 525], radius=18, fill=primary)
        draw.polygon([(205, 165), (255, 118), (470, 118), (435, 165)], fill=highlight)
        draw.polygon([(435, 165), (470, 118), (470, 476), (435, 525)], fill=accent)
        draw.rounded_rectangle([228, 250, 412, 410], radius=16, fill=label_bg)
    elif style == "can":
        # Cylindrical tin with metallic rim
        draw.rounded_rectangle([225, 160, 415, 520], radius=20, fill=primary)
        draw.ellipse([225, 142, 415, 188], fill=(226, 232, 240), outline=(148, 163, 184), width=4)
        draw.ellipse([225, 498, 415, 538], fill=(148, 163, 184))
        draw.rectangle([225, 250, 415, 435], fill=label_bg)
        draw.rectangle([225, 250, 415, 290], fill=accent)
    elif style == "pouch":
        # Pillow snack/confectionery bag
        draw.rounded_rectangle([205, 130, 435, 520], radius=42, fill=primary)
        draw.rectangle([205, 122, 435, 152], fill=accent)
        draw.rectangle([205, 502, 435, 532], fill=accent)
        draw.ellipse([240, 215, 400, 395], fill=label_bg)
        draw.ellipse([268, 245, 372, 365], fill=highlight)
    else:
        # Jug / dispenser (cleaning / personal care)
        draw.rounded_rectangle([290, 96, 355, 142], radius=8, fill=accent)
        draw.rounded_rectangle([205, 146, 425, 528], radius=40, fill=primary)
        draw.rounded_rectangle([355, 200, 396, 330], radius=14, fill=(241, 245, 249))
        draw.rounded_rectangle([225, 250, 345, 435], radius=18, fill=label_bg)
        draw.rectangle([225, 250, 345, 286], fill=accent)

    # Subtle brand seal dot offset uniquely per SKU
    bx = 290 + (rng_bits % 5) * 10 - 20
    draw.ellipse([bx - 16, 358, bx + 16, 390], fill=primary if style != "sack" else label_bg)


def render_product_image(
    *,
    seed_key: str,
    item_id: int,
    name_en: str,
    category: str,
    badge_text: str,
) -> dict[str, Any]:
    """Render a unique 640x640 studio product WebP + thumbnail for an inventory catalog item."""
    h = _seed_hash(seed_key)
    bg_top, primary, accent = _PALETTES[h % len(_PALETTES)]
    # Shift hue gently per item index so adjacent products never look identical
    primary = (
        max(20, min(220, primary[0] + ((h >> 3) % 35) - 17)),
        max(20, min(220, primary[1] + ((h >> 7) % 35) - 17)),
        max(20, min(220, primary[2] + ((h >> 11) % 35) - 17)),
    )
    img = Image.new("RGB", (640, 640), bg_top)
    bottom = tuple(max(195, c - 26) for c in bg_top)
    _draw_soft_gradient(img, bg_top, bottom)
    draw = ImageDraw.Draw(img)

    cat_lower = category.lower()
    if "زيت" in category or "oil" in cat_lower:
        style = "bottle"
    elif "حبوب" in category or "أرز" in category or "grain" in cat_lower or "rice" in cat_lower:
        style = "sack"
    elif "معلب" in category or "can" in cat_lower:
        style = "can"
    elif "مقرمش" in category or "حلو" in category or "snack" in cat_lower:
        style = "pouch"
    elif "منظف" in category or "عناي" in category or "clean" in cat_lower or "care" in cat_lower:
        style = "jug"
    elif "مشروب" in category or "ألبان" in category or "bev" in cat_lower or "dairy" in cat_lower:
        style = "bottle" if (h % 2 == 0) else "carton"
    else:
        style = "carton"

    _packaging_silhouette(draw, style, primary, accent, h)

    # Clean top-right wholesale specification badge (weight/pack)
    f_badge = _font(26)
    f_sub = _font(20)
    short_label = (name_en[:24] + "...") if len(name_en) > 26 else name_en
    draw.rounded_rectangle([420, 32, 608, 86], radius=22, fill=primary)
    draw.text((514, 59), badge_text[:12], fill=(255, 255, 255), font=f_badge, anchor="mm")
    draw.rounded_rectangle([32, 560, 608, 610], radius=16, fill=(255, 255, 255))
    draw.text((320, 585), short_label, fill=(30, 41, 59), font=f_sub, anchor="mm")

    rel_full = f"products/{seed_key.replace(':', '_')}.webp"
    rel_thumb = f"products/thumbs/{seed_key.replace(':', '_')}_thumb.webp"
    storage_key = f"items/{item_id}/catalog.webp"
    raw, sha, w, h_px = _save_webp_pair(img, rel_full, rel_thumb, (220, 220))
    return {
        "asset_id": f"asset-product-{item_id}",
        "entity_type": "product",
        "entity_id": item_id,
        "seed_key": seed_key,
        "file_path": rel_full,
        "thumb_path": rel_thumb,
        "storage_bucket": "product-images",
        "storage_key": storage_key,
        "format": "WEBP",
        "dimensions": {"width": w, "height": h_px},
        "alt_ar": f"صورة منتج {name_en}",
        "alt_en": name_en,
        "source": "synthetic_pillow_studio",
        "license": "synthetic_local_demo_asset",
        "sha256": sha,
        "_bytes": raw,
    }


def render_user_portrait(*, seed_key: str, user_id: int, name: str, role: str) -> dict[str, Any]:
    """Render a crisp studio headshot avatar WebP + thumbnail customized by role and seed."""
    h = _seed_hash(seed_key)
    role_colors = {
        "manager": ((20, 83, 45), (15, 118, 110)),
        "accountant": ((30, 58, 138), (37, 99, 235)),
        "warehouse": ((146, 64, 14), (217, 119, 6)),
        "staff": ((51, 65, 85), (100, 116, 139)),
        "rep": ((13, 148, 136), (6, 95, 70)),
        "shop": ((88, 28, 135), (147, 51, 234)),
    }
    top, accent = role_colors.get(role, ((15, 118, 110), (30, 64, 175)))
    bg_bot = tuple(min(245, c + 120) for c in top)
    img = Image.new("RGB", (512, 512), top)
    _draw_soft_gradient(img, tuple(min(245, c + 150) for c in top), bg_bot)
    d = ImageDraw.Draw(img)

    # Decorative background studio halo
    d.ellipse([76, 66, 436, 426], fill=tuple(min(252, c + 165) for c in accent))
    skin = _SKIN_TONES[h % len(_SKIN_TONES)]
    hair = _HAIR_COLORS[(h >> 4) % len(_HAIR_COLORS)]

    # Shoulders / attire (safety vest for warehouse, suit for manager/accountant, polo for rep/shop)
    d.pieslice([86, 330, 426, 630], 180, 360, fill=top)
    if role in ("warehouse", "staff"):
        d.rectangle([188, 355, 224, 512], fill=(250, 204, 21))
        d.rectangle([288, 355, 324, 512], fill=(250, 204, 21))
    else:
        d.polygon([(220, 345), (256, 405), (292, 345)], fill=(248, 250, 252))

    # Neck & head
    d.rounded_rectangle([226, 285, 286, 358], radius=22, fill=skin)
    d.ellipse([172, 128, 340, 314], fill=skin)

    # Hair / Hijab / Traditional style variation
    style_idx = (h >> 8) % 4
    if style_idx == 0:
        # Clean side-part hair
        d.pieslice([168, 116, 344, 255], 175, 365, fill=hair)
    elif style_idx == 1:
        # Hijab wrap
        d.pieslice([152, 108, 360, 340], 150, 390, outline=accent, width=26)
        d.pieslice([164, 118, 348, 240], 180, 360, fill=accent)
    elif style_idx == 2:
        # Kufiyah / headwear
        d.pieslice([160, 108, 352, 240], 175, 365, fill=(248, 250, 252))
        d.arc([168, 134, 344, 180], 185, 355, fill=(30, 41, 59), width=10)
    else:
        d.chord([170, 116, 342, 232], 180, 360, fill=hair)

    # Eyes + gentle smile + optional beard/glasses
    d.ellipse([212, 206, 226, 220], fill=(30, 41, 59))
    d.ellipse([286, 206, 300, 220], fill=(30, 41, 59))
    d.arc([230, 244, 282, 272], 15, 165, fill=(136, 72, 60), width=4)
    if (h >> 12) % 3 == 0:
        d.rounded_rectangle([200, 198, 244, 228], radius=8, outline=(30, 41, 59), width=3)
        d.rounded_rectangle([268, 198, 312, 228], radius=8, outline=(30, 41, 59), width=3)
        d.line([(244, 212), (268, 212)], fill=(30, 41, 59), width=3)

    # Role pill badge
    f = _font(22)
    d.rounded_rectangle([166, 444, 346, 492], radius=22, fill=(255, 255, 255))
    d.text((256, 468), role.upper(), fill=top, font=f, anchor="mm")

    rel_full = f"users/{seed_key.replace(':', '_')}.webp"
    rel_thumb = f"users/thumbs/{seed_key.replace(':', '_')}_thumb.webp"
    storage_key = f"user-photos/{user_id}.webp"
    raw, sha, w, h_px = _save_webp_pair(img, rel_full, rel_thumb, (160, 160))
    return {
        "asset_id": f"asset-user-{user_id}",
        "entity_type": "user",
        "entity_id": user_id,
        "seed_key": seed_key,
        "file_path": rel_full,
        "thumb_path": rel_thumb,
        "storage_bucket": "product-images",
        "storage_key": storage_key,
        "format": "WEBP",
        "dimensions": {"width": w, "height": h_px},
        "alt_ar": f"صورة حساب {name}",
        "alt_en": f"Portrait for {name} ({role})",
        "source": "synthetic_pillow_studio",
        "license": "synthetic_local_demo_asset",
        "sha256": sha,
        "_bytes": raw,
    }


def render_delivery_proof(*, seed_key: str, order_id: int, order_number: str) -> dict[str, Any]:
    """Render a realistic synthetic delivery proof photo WebP (cartons + delivery tag)."""
    h = _seed_hash(seed_key)
    img = Image.new("RGB", (800, 600), (241, 245, 249))
    _draw_soft_gradient(img, (226, 232, 240), (203, 213, 225))
    d = ImageDraw.Draw(img)
    # Tiled shop entrance floor
    d.polygon([(0, 430), (800, 430), (800, 600), (0, 600)], fill=(148, 163, 184))
    # Stacked brown corrugated wholesale boxes with packing tape & labels
    boxes = [
        (130, 290, 410, 475, (194, 145, 96)),
        (415, 310, 675, 480, (180, 130, 82)),
        (210, 145, 535, 305, (206, 158, 109)),
    ]
    for x1, y1, x2, y2, col in boxes:
        d.rounded_rectangle([x1, y1, x2, y2], radius=8, fill=col, outline=(120, 82, 45), width=3)
        d.rectangle([x1 + 12, y1 + 32, x1 + 115, y1 + 78], fill=(255, 255, 255))
        d.rectangle([(x1 + x2) // 2 - 16, y1, (x1 + x2) // 2 + 16, y2], fill=(214, 185, 140))

    d.rounded_rectangle([40, 32, 420, 96], radius=14, fill=(15, 118, 110))
    d.text((230, 64), f"DELIVERED  {order_number}  #{100 + (h % 899)}", fill=(255, 255, 255), font=_font(24), anchor="mm")

    rel_full = f"deliveries/{seed_key.replace(':', '_')}.webp"
    rel_thumb = f"deliveries/thumbs/{seed_key.replace(':', '_')}_thumb.webp"
    raw, sha, w, h_px = _save_webp_pair(img, rel_full, rel_thumb, (240, 180))
    return {
        "asset_id": f"asset-delivery-{order_id}",
        "entity_type": "delivery",
        "entity_id": order_id,
        "seed_key": seed_key,
        "file_path": rel_full,
        "thumb_path": rel_thumb,
        "storage_bucket": "delivery-proofs",
        "storage_key": f"order-{order_id}/proof-demo.webp",
        "format": "WEBP",
        "dimensions": {"width": w, "height": h_px},
        "alt_ar": f"إثبات تسليم الطلب {order_number}",
        "alt_en": f"Delivery proof for order {order_number}",
        "source": "synthetic_pillow_studio",
        "license": "synthetic_local_demo_asset",
        "sha256": sha,
        "_bytes": raw,
    }


def render_expense_bill(*, seed_key: str, expense_id: int, number: str, category: str, amount: int) -> dict[str, Any]:
    """Render a crisp synthetic Iraqi commercial expense bill/receipt WebP + thumbnail."""
    img = Image.new("RGB", (640, 800), (226, 232, 240))
    d = ImageDraw.Draw(img)
    # Paper voucher card
    d.rounded_rectangle([64, 48, 576, 752], radius=16, fill=(255, 253, 246), outline=(203, 213, 225), width=3)
    d.rectangle([64, 48, 576, 152], fill=(15, 118, 110))
    d.text((320, 88), "AL-NAHRAIN TRADING CO.", fill=(255, 255, 255), font=_font(26), anchor="mm")
    d.text((320, 124), f"EXPENSE VOUCHER  {number}", fill=(204, 251, 241), font=_font(20), anchor="mm")

    f_body = _font(22)
    d.text((100, 210), f"Category: {category.upper()}", fill=(30, 41, 59), font=f_body)
    d.text((100, 258), f"Amount:   {amount:,} IQD", fill=(15, 118, 110), font=_font(28))
    for row_y in range(340, 620, 48):
        d.line([(96, row_y), (544, row_y)], fill=(226, 232, 240), width=2)
    # Stamp circle
    d.ellipse([365, 580, 525, 710], outline=(22, 163, 74), width=5)
    d.text((445, 645), "PAID / DEMO", fill=(22, 163, 74), font=_font(20), anchor="mm")

    rel_full = f"expenses/{seed_key.replace(':', '_')}.webp"
    rel_thumb = f"expenses/thumbs/{seed_key.replace(':', '_')}_thumb.webp"
    raw, sha, w, h_px = _save_webp_pair(img, rel_full, rel_thumb, (192, 240))
    return {
        "asset_id": f"asset-expense-{expense_id}",
        "entity_type": "expense",
        "entity_id": expense_id,
        "seed_key": seed_key,
        "file_path": rel_full,
        "thumb_path": rel_thumb,
        "storage_bucket": "delivery-proofs",
        "storage_key": f"expenses/{expense_id}.webp",
        "format": "WEBP",
        "dimensions": {"width": w, "height": h_px},
        "alt_ar": f"سند صرف {number}",
        "alt_en": f"Expense receipt {number}",
        "source": "synthetic_pillow_studio",
        "license": "synthetic_local_demo_asset",
        "sha256": sha,
        "_bytes": raw,
    }


def register_showcase_assets() -> list[dict[str, Any]]:
    """Convert any AI-generated hero images (warehouse, vehicle, shop, delivery proof) into organized seed_assets WebPs."""
    entries: list[dict[str, Any]] = []
    specs = [
        ("warehouses", ("warehouse_hall_ai.jpg", "warehouse_hall_ai.png"), "wh_hero", "المستودع المركزي - كربلاء", "Karbala Central Warehouse Interior"),
        ("vehicles", ("delivery_van_ai.jpg", "delivery_van_ai.png"), "fleet_van", "شاحنة التوصيل المبردة", "Refrigerated Delivery Van"),
        ("customers", ("shop_hero_ai.jpg", "shop_hero_ai.png"), "shop_aisle", "متجر جملة نموذجي", "Wholesale Supermarket Display"),
        ("deliveries", ("delivery_proof_ai.jpg", "delivery_proof_ai.png"), "proof_hero", "تسليم طلبات جملة موثق", "Documented Wholesale Delivery Drop"),
    ]
    for folder, src_names, key, alt_ar, alt_en in specs:
        src_path = next((p for n in src_names if (p := SEED_ASSETS_DIR / folder / n).is_file()), None)
        if src_path is None:
            img = Image.new("RGB", (800, 600), (240, 249, 246))
            d = ImageDraw.Draw(img)
            d.rounded_rectangle([40, 40, 760, 560], radius=24, fill=(15, 118, 110))
            d.text((400, 300), alt_en, fill=(255, 255, 255), font=_font(30), anchor="mm")
            source = "synthetic_pillow_studio"
        else:
            img = Image.open(src_path).convert("RGB")
            img.thumbnail((1024, 1024), Image.Resampling.LANCZOS)
            source = "ai_generated"
        rel_full = f"{folder}/{key}.webp"
        rel_thumb = f"{folder}/thumbs/{key}_thumb.webp"
        _, sha, w, h_px = _save_webp_pair(img, rel_full, rel_thumb, (240, 180))
        entries.append(
            {
                "asset_id": f"asset-showcase-{key}",
                "entity_type": folder.rstrip("s"),
                "entity_id": None,
                "seed_key": f"showcase:{key}",
                "file_path": rel_full,
                "thumb_path": rel_thumb,
                "storage_bucket": None,
                "storage_key": None,
                "format": "WEBP",
                "dimensions": {"width": w, "height": h_px},
                "alt_ar": alt_ar,
                "alt_en": alt_en,
                "source": source,
                "license": "synthetic_local_demo_asset",
                "sha256": sha,
            }
        )
    return entries


def write_asset_manifest(entries: list[dict[str, Any]]) -> Path:
    """Persist `backend/seed_assets/asset_manifest.json` without internal `_bytes` buffers."""
    SEED_ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    clean_entries = [{k: v for k, v in e.items() if k != "_bytes"} for e in entries]
    out = {
        "generated_at_policy": "deterministic_seed",
        "total_assets": len(clean_entries),
        "assets": clean_entries,
    }
    manifest_path = SEED_ASSETS_DIR / "asset_manifest.json"
    manifest_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest_path
