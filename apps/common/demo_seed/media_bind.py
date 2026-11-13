"""Bind deterministic WebP media assets into Django StorageBackend and emit asset_manifest.json."""
from __future__ import annotations

import json
from typing import Any

from apps.accounts.models import SiteConfig, User
from apps.catalog.models import Item
from apps.common.demo_media import (
    SEED_ASSETS_DIR,
    register_showcase_assets,
    render_company_logo,
    render_product_image,
    render_user_portrait,
    write_asset_manifest,
)
from apps.common.demo_seed.registry import register_seed
from integrations.storage import get_storage


def bind_demo_media(
    *,
    items: list[Item],
    users: list[User],
    logo_meta: dict[str, Any] | None = None,
    proof_assets: list[dict[str, Any]] | None = None,
    expense_assets: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Generate/store WebP assets for items, users, company logo, proofs, and write asset_manifest.json."""
    storage = get_storage()
    entries: list[dict[str, Any]] = []

    if logo_meta is None:
        logo_meta = render_company_logo("company:logo")
    cfg = SiteConfig.get_solo()
    if not cfg.brand_logo_path:
        cfg.brand_logo_path = storage.put_bytes(
            "product-images",
            logo_meta["storage_key"],
            logo_meta["_bytes"],
            "image/webp",
        )
        cfg.save(update_fields=["brand_logo_path"])
    logo_meta["storage_key"] = cfg.brand_logo_path
    entries.append(logo_meta)

    # All 96 catalog items -> unique 640x640 packaging WebP + 220x220 thumb
    for it in items:
        badge = f"{it.pieces_per_carton}x PCS" if it.pieces_per_carton > 1 else "1 UNIT"
        meta = render_product_image(
            seed_key=f"item:{it.id}",
            item_id=it.id,
            name_en=it.name_en,
            category=it.category,
            badge_text=badge,
        )
        stored_key = storage.put_bytes("product-images", meta["storage_key"], meta["_bytes"], "image/webp")
        if it.image_path != stored_key:
            it.image_path = stored_key
            it.save(update_fields=["image_path"])
        meta["storage_key"] = stored_key
        register_seed(f"media:item:{it.id}", it, extra={"bucket": "product-images", "key": stored_key})
        entries.append(meta)

    # All 70 users -> unique role-styled portrait WebP + 160x160 thumb
    for u in users:
        meta = render_user_portrait(seed_key=f"user:{u.id}", user_id=u.id, name=u.name, role=u.role)
        stored_key = storage.put_bytes("product-images", meta["storage_key"], meta["_bytes"], "image/webp")
        if u.photo_path != stored_key:
            u.photo_path = stored_key
            u.save(update_fields=["photo_path"])
        meta["storage_key"] = stored_key
        register_seed(f"media:user:{u.id}", u, extra={"bucket": "product-images", "key": stored_key})
        entries.append(meta)

    if proof_assets is None or expense_assets is None:
        existing_manifest = SEED_ASSETS_DIR / "asset_manifest.json"
        if existing_manifest.is_file():
            try:
                old_assets = json.loads(existing_manifest.read_text(encoding="utf-8")).get("assets", [])
                if proof_assets is None:
                    proof_assets = [a for a in old_assets if a.get("entity_type") == "delivery"]
                if expense_assets is None:
                    expense_assets = [a for a in old_assets if a.get("entity_type") == "expense"]
            except Exception:
                pass

    entries.extend(proof_assets or [])
    entries.extend(expense_assets or [])
    entries.extend(register_showcase_assets())

    manifest_path = write_asset_manifest(entries)
    return {
        "total_assets": len(entries),
        "manifest_path": str(manifest_path),
    }
