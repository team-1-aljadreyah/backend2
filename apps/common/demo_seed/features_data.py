"""Seed extended ERP features: counter sales, custody handovers, visits, clearances, price schedules, alerts, ratings, and shop lists."""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any

from apps.accounts.models import PriceTier, Role, User
from apps.catalog.features_services import (
    cancel_clearance,
    cancel_scheduled_price,
    create_scheduled_price,
    set_stock_alert,
    start_clearance,
)
from apps.catalog.models import Item
from apps.common.demo_seed.registry import frozen_now, get_seeded, register_seed
from apps.common.utils import baghdad_now, baghdad_today
from apps.customers.models import Customer, ShopMembership
from apps.customers.services import customer_balance_map, save_shop_list
from apps.customers.visits_services import js_weekday, record_visit, set_customer_visit_days
from apps.inventory.models import Batch
from apps.invoices.models import Invoice
from apps.invoices.services import record_counter_sale
from apps.orders.models import Order
from apps.orders.services import rate_order_delivery
from apps.payments.custody_services import (
    cancel_cash_handover,
    confirm_cash_handover,
    create_cash_handover,
    list_custody_for_actor,
)
from apps.payments.models import CashHandover, Payment, PaymentMethod
from apps.payments.services import create_payment
from apps.warehouses.models import Warehouse


def _hist_dt(days_ago: int, hour: int = 11, minute: int = 0):
    now = baghdad_now()
    target = (now - timedelta(days=days_ago)).replace(hour=hour, minute=minute, second=0, microsecond=0)
    return target if target < now else now - timedelta(minutes=15)


def seed_extended_features(
    *,
    manager: User,
    accountants: list[User],
    keepers: list[User],
    sales_reps: list[User],
    delivery_reps: list[User],
    warehouses: list[Warehouse],
    items: list[Item],
    active_customers: list[Customer],
    delivered_orders: list[Order],
    shop_users: list[User],
    fresh_customers: list[Customer],
) -> dict[str, Any]:
    """Populate counter sales, custody handovers, rep visits, clearances, dated prices, stock alerts, ratings, and saved shop lists."""
    today = baghdad_today()
    tiers = list(PriceTier.objects.order_by("sort_order", "id"))
    default_tier = tiers[min(1, len(tiers) - 1)]

    # 1. Counter Sales (4 direct warehouse sales: 2 in Main Al-Sina'a + 2 in Second Al-Hur)
    counter_count = 0
    counter_specs = [
        (1, keepers[0], warehouses[0], "cash", "أبو كرار لأسواق الجملة", "07712345601", items[0], items[10]),
        (2, keepers[0], warehouses[0], "transfer", "أسواق النهرين الشعبية", "07712345602", items[30], items[50]),
        (3, keepers[1], warehouses[1], "cash", "سوبرماركت حي الحر", "07712345603", items[20], items[40]),
        (4, keepers[1], warehouses[1], "cash", "بقالة السلامة المباشرة", "07712345604", items[22], items[42]),
    ]
    for c_idx, k_actor, wh, method, b_name, b_phone, it_a, it_b in counter_specs:
        dt = _hist_dt(max(1, 7 - c_idx), hour=10 + c_idx)
        cref = f"seed-counter-{c_idx}"
        if not Order.objects.filter(client_ref=cref).exists():
            with frozen_now(dt):
                inv_res = record_counter_sale(
                    actor=k_actor,
                    data={
                        "warehouse_id": wh.id,
                        "tier_id": default_tier.id,
                        "method": method,
                        "buyer_name": b_name,
                        "buyer_phone": b_phone,
                        "client_ref": cref,
                        "note": f"مبيع مباشر من كاونتر {wh.name}",
                        "lines": [
                            {"item_id": it_a.id, "unit": "carton", "qty": 2},
                            {"item_id": it_b.id, "unit": "carton", "qty": 1},
                        ],
                    },
                )
            inv_obj = Invoice.objects.get(id=inv_res["id"])
            register_seed(f"inv:{inv_obj.id}", inv_obj)
            if inv_obj.order:
                register_seed(f"ord:counter:{c_idx}", inv_obj.order)
            counter_count += 1
    walk_in = Customer.objects.filter(walk_in=True).first()
    if walk_in:
        register_seed("cust:walkin", walk_in)

    # 2. Clearances (2 active clearances on near-expiry batches across WH1 & WH2 + 1 cancelled clearance)
    clr_count = 0
    used_clr_items: set[int] = set()
    for wh_idx, pct in ((0, 25), (1, 35), (0, 20)):
        cand = (
            Batch.objects.filter(
                warehouse_id=warehouses[wh_idx].id,
                qty_left__gt=4,
                held_at__isnull=True,
                expiry_date__gt=today + timedelta(days=2),
                expiry_date__lte=today + timedelta(days=45),
            )
            .exclude(item_id__in=used_clr_items)
            .order_by("expiry_date", "id")
            .first()
        )
        if not cand:
            continue
        used_clr_items.add(cand.item_id)
        res_c = start_clearance(actor=manager, data={"batch_id": cand.id, "percent": pct})
        if clr_count == 2:
            cancel_clearance(actor=manager, clearance_id=res_c["id"])
        clr_count += 1

    # 3. Dated Price Schedules (2 scheduled + 1 cancelled)
    sched_specs = [
        ([it.id for it in items[0:3]], (today + timedelta(days=3)).isoformat(), 5, "تحديث أسعار زيوت دوار الشمس الموسمية (+5%)", False),
        ([it.id for it in items[78:81]], (today + timedelta(days=7)).isoformat(), -5, "عرض تخفيض أسبوعي على البسكويت والحلويات (-5%)", False),
        ([it.id for it in items[20:22]], (today + timedelta(days=10)).isoformat(), 8, "مراجعة أسعار الألبان (ملغى)", True),
    ]
    sched_count = 0
    for s_items, s_day, pct_val, note, do_cancel in sched_specs:
        res_s = create_scheduled_price(
            actor=manager,
            data={"item_ids": s_items, "start": s_day, "percent": pct_val, "note": note},
        )
        if do_cancel:
            cancel_scheduled_price(actor=manager, schedule_id=res_s["id"])
        sched_count += 1

    # 4. Back-in-Stock Waitlist Alerts on out-of-stock/low-stock items (items[91..94])
    alert_count = 0
    for idx_u, su in enumerate(shop_users[:6]):
        target_item = items[93 if idx_u % 2 == 0 else 94]
        try:
            set_stock_alert(actor=su, item_id=target_item.id, on=True)
            alert_count += 1
        except Exception:
            pass
    for su in shop_users[:2]:
        try:
            set_stock_alert(actor=su, item_id=items[91].id, on=True)
            alert_count += 1
        except Exception:
            pass

    # 5. Customer Visit Days (line of travel) + Rep Visits across outcomes & days
    today_js_wd = js_weekday(today)
    other_wd = (today_js_wd + 2) % 7
    alt_wd = (today_js_wd + 4) % 7
    visit_count = 0
    for c_idx, cust in enumerate(active_customers[:24]):
        days = [today_js_wd, other_wd] if c_idx < 12 else [other_wd, alt_wd]
        set_customer_visit_days(actor=manager, customer_id=cust.id, raw_days=days)

    outcomes_cycle = [
        ("order", "", "تم الاتفاق على طلبية أسبوعية جديدة"),
        ("payment", "", "تم استلام دفعة نقدية من المتجر"),
        ("no_order", "stocked", "المخزون متوفر لدى المتجر حالياً"),
        ("no_order", "price", "بانتظار موافقة خصم الكميات"),
        ("closed", "", "المتجر مغلق وقت الزيارة المسائية"),
    ]
    for v_idx, cust in enumerate(fresh_customers[:16]):
        rep_actor = cust.rep if (cust.rep and cust.rep.role == Role.REP and cust.rep.active) else sales_reps[v_idx % len(sales_reps)]
        if cust.rep_id != rep_actor.id:
            continue
        day_offset = 0 if v_idx < 6 else (1 + (v_idx % 5))
        v_dt = _hist_dt(day_offset, hour=9 + (v_idx % 6), minute=15)
        outcome, reason, note = outcomes_cycle[v_idx % len(outcomes_cycle)]
        lat = float(cust.lat) if cust.lat is not None else 32.615
        lng = float(cust.lng) if cust.lng is not None else 44.025
        # Showcase 1 far visit warning (offset GPS > 200m) cleanly
        if v_idx == 2:
            lat += 0.0045
        with frozen_now(v_dt):
            record_visit(
                actor=rep_actor,
                data={
                    "customer_id": cust.id,
                    "outcome": outcome,
                    "reason": reason,
                    "note": note,
                    "lat": lat,
                    "lng": lng,
                },
            )
            visit_count += 1

    # 6. Cash Custody & Handovers (confirmed exact, confirmed w/ short, pending, cancelled)
    # Pick customers who actually owe open debt so field collections settle real invoices without creating bogus negative balances.
    debt_map = customer_balance_map([c.id for c in fresh_customers if not c.walk_in])
    debtor_pool = sorted(
        [c for c in fresh_customers if not c.walk_in and debt_map.get(c.id, 0) >= 50_000],
        key=lambda c: (-debt_map.get(c.id, 0), c.id),
    )
    for rep_idx, rep in enumerate(sales_reps[:3] + delivery_reps[:2]):
        rep_cust = next((c for c in debtor_pool if c.rep_id == rep.id and debt_map.get(c.id, 0) >= 50_000), None)
        if rep_cust is None and debtor_pool:
            rep_cust = max(debtor_pool, key=lambda c: debt_map.get(c.id, 0))
        if rep_cust is None or debt_map.get(rep_cust.id, 0) < 1_000:
            continue
        cur_debt = debt_map.get(rep_cust.id, 0)
        collect_amt = min(cur_debt, 200_000 + rep_idx * 50_000)
        collect_amt = max(250, (collect_amt // 250) * 250)
        c_dt = _hist_dt(3 + rep_idx, hour=14)
        with frozen_now(c_dt):
            try:
                p_res = create_payment(
                    actor=rep if rep_cust.rep_id == rep.id else accountants[0],
                    data={
                        "customer_id": rep_cust.id,
                        "amount": collect_amt,
                        "method": PaymentMethod.CASH,
                        "note": f"تحصيل نقدي ميداني بعهدة {rep.name}",
                        "client_ref": f"seed-custody-cash-{rep.id}",
                    },
                )
                debt_map[rep_cust.id] = cur_debt - collect_amt
                p_obj = Payment.objects.get(id=p_res["id"])
                if p_obj.received_by_id != rep.id:
                    p_obj.received_by = rep
                    p_obj.save(update_fields=["received_by"])
                register_seed(f"pay:custody:{rep.id}", p_obj)
            except Exception:
                pass

    handover_count = 0
    custody_rows = [r for r in list_custody_for_actor(actor=manager) if int(r.get("holding") or 0) >= 200_000]
    for h_idx, row in enumerate(custody_rows[:4]):
        rep_obj = User.objects.get(id=row["rep"]["id"])
        if CashHandover.objects.filter(rep=rep_obj, status="pending").exists():
            continue
        hold_amt = int(row["holding"])
        decl_amt = min(hold_amt, max(150_000, ((hold_amt // 2) // 250) * 250))
        h_dt = _hist_dt(max(0, 3 - h_idx), hour=16)
        with frozen_now(h_dt):
            h_res = create_cash_handover(
                actor=rep_obj,
                data={"amount": decl_amt, "note": f"تسليم عهدة نقدية #{h_idx + 1}"},
            )
            if h_idx == 0:
                confirm_cash_handover(actor=accountants[0], handover_id=h_res["id"], received_raw=decl_amt)
            elif h_idx == 1 and decl_amt > 25_000:
                confirm_cash_handover(actor=accountants[0], handover_id=h_res["id"], received_raw=decl_amt - 25_000)
            elif h_idx == 3:
                cancel_cash_handover(actor=rep_obj, handover_id=h_res["id"])
            # h_idx == 2 stays PENDING confirmation in Accountant/Manager queue!
            handover_count += 1

    # 7. Delivery Ratings (10 delivered orders across 1-5 stars by shop owners within 7 days of delivery)
    rating_count = 0
    rating_samples = [
        (5, "خدمة توصيل ممتازة ومواعيد دقيقة جداً"),
        (5, "المنتجات وصلت بحالة ممتازة وتغليف نظيف"),
        (4, "المندوب متعاون والطلب كامل"),
        (5, "شكراً لفريق مخزن الصناعة على سرعة التجهيز"),
        (3, "التوصيل تأخر ساعة عن الموعد المحدد"),
        (5, "أفضل تعامل جملة في كربلاء"),
        (2, "تأخر التوصيل للمساء وكان كرتون واحد غير مرتب"),
        (4, "ممتاز، نتمنى الاستمرار بهذا المستوى"),
        (1, "نقص في أحد الأصناف وتأخير في الرد عند الاستلام"),
        (5, "توصيل سريع جداً من مخزن الحر"),
    ]
    for ord_obj in reversed(delivered_orders):
        if rating_count >= len(rating_samples):
            break
        ms = ShopMembership.objects.filter(
            customer_id=ord_obj.customer_id, is_active_shop=True, user__active=True
        ).select_related("user").first()
        if not ms:
            continue
        stars, note = rating_samples[rating_count]
        dlv = getattr(ord_obj, "delivery", None)
        base_dt = (dlv.delivered_at if dlv and dlv.delivered_at else ord_obj.created_at)
        r_dt = base_dt + timedelta(hours=6)
        with frozen_now(r_dt):
            try:
                rate_order_delivery(actor=ms.user, order_id=ord_obj.id, data={"stars": stars, "note": note})
                rating_count += 1
            except Exception:
                continue

    # 8. Saved Shop Lists (Shop owners quick-reorder favorites)
    shoplist_count = 0
    list_templates = [
        ("مشتريات الأسبوع الأساسية", [(0, "carton", 4), (10, "carton", 3), (20, "carton", 5)]),
        ("طلبيات الألبان والمشروبات", [(21, "carton", 4), (40, "carton", 10), (42, "carton", 4)]),
        ("مواد التنظيف والعناية", [(59, "carton", 3), (60, "carton", 4), (69, "carton", 2)]),
    ]
    for su_idx, su in enumerate(shop_users[:4]):
        lname, spec_lines = list_templates[su_idx % len(list_templates)]
        try:
            save_shop_list(
                actor=su,
                list_id=None,
                data={
                    "name": lname,
                    "lines": [{"item_id": items[it_i].id, "unit": u, "qty": q} for it_i, u, q in spec_lines],
                },
            )
            shoplist_count += 1
        except Exception:
            pass

    return {
        "counter_sales": counter_count,
        "clearances": clr_count,
        "scheduled_prices": sched_count,
        "stock_alerts": alert_count,
        "visits": visit_count,
        "handovers": handover_count,
        "ratings": rating_count,
        "shop_lists": shoplist_count,
    }
