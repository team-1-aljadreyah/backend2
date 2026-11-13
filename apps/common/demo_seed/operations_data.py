"""90-day historical & live ERP operational workflows executed through real domain services."""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

from apps.accounts.models import Role, User
from apps.catalog.models import Item
from apps.chat.services import mark_chat_read, send_customer_chat_message, send_my_chat_message
from apps.commissions.models import ClosedCommissionMonth
from apps.commissions.services import close_commission_month
from apps.common.demo_media import (
    bytes_to_data_url,
    render_delivery_proof,
    render_expense_bill,
)
from apps.common.demo_seed.catalog_data import CATEGORIES
from apps.common.demo_seed.registry import frozen_now, get_seeded, register_raw_seed, register_seed
from apps.common.utils import baghdad_now, baghdad_today, decrypt_secret
from apps.customers.models import Customer
from apps.deliveries.models import Delivery, Vehicle
from apps.deliveries.services import (
    deliver_order,
    dispatch_delivery,
    load_shipment,
    report_delivery_problem,
)
from apps.expenses.models import Expense
from apps.expenses.services import cancel_expense, create_expense, decide_expense
from apps.inventory.models import Batch
from apps.inventory.services import hold_batch
from apps.invoices.models import Invoice, ReturnDoc
from apps.invoices.services import create_credit_note, create_return
from apps.notifications.tasks import process_outbox_events_task
from apps.orders.models import Order, OrderStatus
from apps.orders.services import (
    accept_order,
    approve_order,
    cancel_order,
    create_order,
    plan_shipments,
    prepare_order,
)
from apps.payments.models import OnlinePayment, OnlinePaymentStatus, Payment, PaymentMethod
from apps.payments.services import create_payment, create_refund, reverse_payment
from apps.receipts.models import Receipt
from apps.receipts.services import create_receipt
from apps.stocktakes.models import Stocktake
from apps.stocktakes.services import close_stocktake, create_stocktake, update_stocktake
from apps.supply_orders.models import SupplyOrder
from apps.supply_orders.services import cancel_supply_order, create_supply_order
from apps.transfers.models import Transfer, TransferStatus
from apps.transfers.services import create_transfer, receive_transfer, review_transfer
from apps.warehouses.models import Warehouse

SUPPLIERS = [
    "شركة البصرة للاستيراد والتجارة العامة",
    "مصانع النهرين للأغذية — بغداد",
    "مجموعة الرافدين للمواد الاستهلاكية",
    "شركة أنوار كربلاء للتجهيز التجاري",
    "مؤسسة دجلة الذهبية للاستيراد",
    "شركة الموانئ العراقية للتجارة",
]


def _hist_dt(days_ago: int, hour: int = 11, minute: int = 15) -> datetime:
    """Build a deterministic UTC datetime matching a Baghdad business hour `days_ago` earlier."""
    base_local = baghdad_now() - timedelta(days=days_ago)
    local_dt = base_local.replace(hour=hour % 24, minute=minute % 60, second=0, microsecond=0)
    return local_dt.astimezone(UTC)


def _prev_month_str(ref_date: date, months_back: int) -> str:
    y, m = ref_date.year, ref_date.month
    total = (y * 12 + (m - 1)) - months_back
    ny, nm = divmod(total, 12)
    return f"{ny:04d}-{nm + 1:02d}"


def seed_operations(
    *,
    manager: User,
    accountants: list[User],
    keepers: list[User],
    staff_users: list[User],
    sales_reps: list[User],
    delivery_reps: list[User],
    warehouses: list[Warehouse],
    items: list[Item],
    item_costs: dict[int, int],
    active_customers: list[Customer],
    vehicles: list[Vehicle],
    with_media: bool = False,
) -> dict[str, Any]:
    """Execute realistic supply, inventory, orders, deliveries, financials, commissions, expenses & chat."""
    # Guard against re-running historical batch creation when full operational seed already exists
    if get_seeded("ops:done", Order):
        return {"already_seeded": True}

    today = baghdad_today()
    proof_assets: list[dict[str, Any]] = []
    expense_assets: list[dict[str, Any]] = []
    # Reuse a single sample photo payload template when generating quick delivery/expense proofs during flow,
    # or generate distinct WebPs per record when with_media=True.
    fallback_proof = bytes_to_data_url(
        render_delivery_proof(seed_key="delivery:template", order_id=0, order_number="ORD-DEMO")["_bytes"],
        "image/webp",
    )

    # ------------------------------------------------------------------
    # 1. Supply Orders (10 total: linked, open pending, partial, cancelled)
    # ------------------------------------------------------------------
    supply_orders: list[SupplyOrder] = []
    for idx in range(1, 11):
        sk = f"supply:{idx:02d}"
        so = get_seeded(sk, SupplyOrder)
        if not so:
            wh = warehouses[(idx - 1) % len(warehouses)]
            s_items = items[((idx - 1) * 6) % 70 : ((idx - 1) * 6) % 70 + 4]
            so_lines = [
                {
                    "item_id": it.id,
                    "unit": "carton",
                    "qty": 30 + (idx * 2),
                    "price": item_costs[it.id] * it.pieces_per_carton,
                }
                for it in s_items
            ]
            dt = _hist_dt(85 - idx * 6, hour=10)
            with frozen_now(dt):
                res = create_supply_order(
                    actor=manager,
                    data={
                        "warehouse_id": wh.id,
                        "supplier": SUPPLIERS[(idx - 1) % len(SUPPLIERS)],
                        "expected_date": (today + timedelta(days=5 + idx)).isoformat(),
                        "notes": f"طلبية تجهيز مخزنية رقم {idx} — دفعة شهرية",
                        "lines": so_lines,
                    },
                )
                so = SupplyOrder.objects.get(id=res["id"])
                if idx == 10:
                    cancel_supply_order(actor=manager, supply_order_id=so.id, reason="تأخر المورّد عن موعد الشحن المتفق عليه")
                    so.refresh_from_db()
            SupplyOrder.objects.filter(id=so.id).update(created_at=dt)
        register_seed(sk, so)
        supply_orders.append(so)

    # ------------------------------------------------------------------
    # 2. Receipts & FEFO Batches (linked to supply_orders 1..7 + full 5-warehouse coverage)
    # ------------------------------------------------------------------
    created_batches: list[Batch] = []
    receipts: list[Receipt] = []

    # 2a: Direct linked receipts fulfilling Supply Orders 1..6 completely and #7 partially
    for s_idx, so in enumerate(supply_orders[:7], start=1):
        rsk = f"rcpt:so:{s_idx:02d}"
        r_obj = get_seeded(rsk, Receipt)
        if not r_obj:
            dt = _hist_dt(84 - s_idx * 5, hour=11)
            k_actor = keepers[(so.warehouse_id - warehouses[0].id) % len(keepers)] if warehouses else keepers[0]
            so_lines = list(so.lines.select_related("item").all())
            rec_lines = []
            for ln in so_lines:
                q_rec = max(1, ln.qty // 2) if s_idx == 7 else ln.qty
                rec_lines.append(
                    {
                        "item_id": ln.item_id,
                        "unit": ln.unit,
                        "qty": q_rec,
                        "purchase_price": item_costs[ln.item_id],
                        "expiry_date": (today + timedelta(days=300 + s_idx * 10)).isoformat(),
                    }
                )
            with frozen_now(dt):
                res = create_receipt(
                    actor=k_actor,
                    data={
                        "warehouse_id": so.warehouse_id,
                        "supply_order_id": so.id,
                        "supplier": so.supplier,
                        "client_ref": f"seed-{rsk}",
                        "lines": rec_lines,
                    },
                )
            r_obj = Receipt.objects.prefetch_related("lines__created_batch").get(id=res["id"])
            Receipt.objects.filter(id=r_obj.id).update(created_at=dt)
        for rl in r_obj.lines.all():
            if rl.created_batch:
                register_seed(f"batch:{rl.created_batch.id}", rl.created_batch)
                created_batches.append(rl.created_batch)
        register_seed(rsk, r_obj)
        receipts.append(r_obj)

    # Step 2b: Comprehensive stock-in across WH1 (all 96 items), WH2 (0..49), WH3 (0..71), WH4 (0..79), WH5 (0..89)
    receipt_specs: list[tuple[str, Warehouse, list[Item], int, int, int | None, int]] = []
    # Chunk all 96 items into receipts so each receipt is crisp and readable (16 lines each)
    for c_idx in range(6):
        chunk = items[c_idx * 16 : (c_idx + 1) * 16]
        receipt_specs.append((f"rcpt:main:{c_idx + 1}", warehouses[0], chunk, 90 - c_idx * 2, 260 + c_idx * 25, None, 1))
    # WH2 Cold & secondary hub (dairy/beverages/tea/oils)
    receipt_specs.append(("rcpt:cold:1", warehouses[1], items[20:50], 78, 190, None, 2))
    receipt_specs.append(("rcpt:cold:2", warehouses[1], items[0:20], 68, 220, None, 2))
    # WH3 Najaf (covers fast items 0..71)
    receipt_specs.append(("rcpt:njf:1", warehouses[2], items[0:36], 74, 240, None, 3))
    receipt_specs.append(("rcpt:njf:2", warehouses[2], items[36:72], 64, 270, None, 3))
    # WH4 Baghdad (covers 0..79)
    receipt_specs.append(("rcpt:bgd:1", warehouses[3], items[0:40], 72, 250, None, 4))
    receipt_specs.append(("rcpt:bgd:2", warehouses[3], items[40:80], 60, 290, None, 4))
    # WH5 Hillah (covers fast items 0..71 + 72..89)
    receipt_specs.append(("rcpt:bab:1", warehouses[4], items[0:45], 66, 230, None, 5))
    receipt_specs.append(("rcpt:bab:2", warehouses[4], items[45:90], 55, 280, None, 5))
    # Short-expiry batches (15-35 days ahead) & future expired test batch candidates
    receipt_specs.append(("rcpt:nearexp:1", warehouses[0], items[0:6], 58, 22, None, 1))
    receipt_specs.append(("rcpt:nearexp:2", warehouses[1], items[20:26], 52, 16, None, 2))
    receipt_specs.append(("rcpt:nearexp:3", warehouses[3], items[40:45], 45, 29, None, 4))
    # Replenishment fresh batches across days -35..-5
    for r_i, wh in enumerate(warehouses, start=1):
        slice_items = items[(r_i - 1) * 10 : (r_i - 1) * 10 + 10]
        receipt_specs.append((f"rcpt:repl:{r_i}", wh, slice_items, 35 - r_i * 5, 340 + r_i * 20, None, r_i))

    # Leave items[91..92] intentionally low stock, items[93..95] with light or zero replenishment
    for sk, wh, batch_items, days_ago, exp_days_from_today, so_id, _kidx in receipt_specs:
        r_obj = get_seeded(sk, Receipt)
        if not r_obj:
            dt = _hist_dt(days_ago, hour=9, minute=30)
            actor_k = keepers[min(_kidx - 1, len(keepers) - 1)]
            lines_payload = []
            for idx_it, it in enumerate(batch_items):
                # Keep items[91..92] at low quantity (below min_stock), items[93..95] skipped on main extra hubs
                if it.id in {items[91].id, items[92].id}:
                    unit_choice = "piece"
                    q_val = max(4, it.min_stock // 3)
                elif "nearexp" in sk:
                    unit_choice = "carton"
                    q_val = 8
                else:
                    unit_choice = "carton"
                    q_val = 28 + ((it.id + idx_it) % 35)
                exp_dt = today + timedelta(days=exp_days_from_today + (idx_it % 12))
                lines_payload.append(
                    {
                        "item_id": it.id,
                        "unit": unit_choice,
                        "qty": q_val,
                        "purchase_price": item_costs[it.id],
                        "expiry_date": exp_dt.isoformat(),
                    }
                )
            with frozen_now(dt):
                res = create_receipt(
                    actor=actor_k,
                    data={
                        "warehouse_id": wh.id,
                        "supply_order_id": so_id,
                        "supplier": SUPPLIERS[(wh.id + len(receipts)) % len(SUPPLIERS)],
                        "client_ref": f"seed-{sk}",
                        "lines": lines_payload,
                    },
                )
            r_obj = Receipt.objects.prefetch_related("lines__created_batch").get(id=res["id"])
            Receipt.objects.filter(id=r_obj.id).update(created_at=dt)
        for rl in r_obj.lines.all():
            if rl.created_batch:
                register_seed(f"batch:{rl.created_batch.id}", rl.created_batch)
                created_batches.append(rl.created_batch)
        register_seed(sk, r_obj)
        receipts.append(r_obj)

    # Create 2 genuinely expired batches (received previously, now past expiry_date) so near/expired reports show them
    for b_idx, exp_it in enumerate((items[88], items[89]), start=1):
        esk = f"rcpt:expired:{b_idx}"
        r_exp = get_seeded(esk, Receipt)
        if not r_exp:
            b_dt = _hist_dt(82, hour=10)
            with frozen_now(b_dt):
                res = create_receipt(
                    actor=keepers[0],
                    data={
                        "warehouse_id": warehouses[0].id,
                        "supplier": SUPPLIERS[0],
                        "client_ref": f"seed-rcpt-expired-{b_idx}",
                        "lines": [
                            {
                                "item_id": exp_it.id,
                                "unit": "piece",
                                "qty": 18,
                                "purchase_price": item_costs[exp_it.id],
                                "expiry_date": (today + timedelta(days=1)).isoformat(),
                            }
                        ],
                    },
                )
            r_exp = Receipt.objects.prefetch_related("lines__created_batch").get(id=res["id"])
            Receipt.objects.filter(id=r_exp.id).update(created_at=b_dt)
            register_seed(esk, r_exp)
            for rl in r_exp.lines.all():
                if rl.created_batch:
                    rl.created_batch.expiry_date = today - timedelta(days=6 + b_idx * 4)
                    rl.created_batch.save(update_fields=["expiry_date"])
                    register_seed(f"batch:{rl.created_batch.id}", rl.created_batch)

    # ------------------------------------------------------------------
    # 3. Inter-Warehouse Transfers (14 transfers: received, discrepancy, in-transit sent)
    # ------------------------------------------------------------------
    # Transfer #1 first so it creates linked sibling lots we can demonstrate `hold_batch` on later!
    shared_lot_batch_id: int | None = None
    for tidx in range(1, 15):
        tkey = f"trf:{tidx:02d}"
        from_wh = warehouses[0] if tidx <= 9 else warehouses[(tidx % 3) + 1]
        to_wh = warehouses[(tidx % 4) + 1] if from_wh.id == warehouses[0].id else warehouses[0]
        if from_wh.id == to_wh.id:
            to_wh = warehouses[2]
        t_items = [items[(tidx * 3 + k) % 35] for k in range(2)]
        t_dt = _hist_dt(50 - tidx * 3, hour=12)
        k_from = keepers[warehouses.index(from_wh)]
        k_to = keepers[warehouses.index(to_wh)]

        trf = get_seeded(tkey, Transfer) or Transfer.objects.filter(client_ref=f"seed-{tkey}").first()
        if not trf:
            with frozen_now(t_dt):
                t_res = create_transfer(
                    actor=k_from,
                    data={
                        "from_warehouse_id": from_wh.id,
                        "to_warehouse_id": to_wh.id,
                        "reason": f"تعزيز مخزون {to_wh.name} بناءً على خطة التوزيع الإقليمي #{tidx}",
                        "client_ref": f"seed-{tkey}",
                        "lines": [{"item_id": it.id, "unit": "carton", "qty": 4} for it in t_items],
                    },
                )
            trf = Transfer.objects.prefetch_related("lines").get(id=t_res["id"])
            Transfer.objects.filter(id=trf.id).update(created_at=t_dt)

        if tidx <= 11 and trf.status == TransferStatus.SENT:
            with frozen_now(t_dt + timedelta(hours=5)):
                received_map: dict[str, int] = {}
                for l_idx, ln in enumerate(trf.lines.order_by("id")):
                    if tidx in (9, 10, 11) and l_idx == 0:
                        received_map[str(ln.item_id)] = max(1, ln.pieces - 2)
                    else:
                        received_map[str(ln.item_id)] = ln.pieces
                receive_transfer(
                    actor=k_to,
                    transfer_id=trf.id,
                    data={
                        "received": received_map,
                        "reason": "نقص طفيف بالكارتون عند الفرز الميداني" if tidx in (9, 10, 11) else "",
                    },
                )
                if tidx in (9, 10):
                    review_transfer(actor=manager, transfer_id=trf.id)
                # tidx == 11 stays status=DISCREPANCY pending review!
        if tidx == 1:
            first_child = Batch.objects.filter(warehouse=to_wh, origin_batch__isnull=False).order_by("-id").first()
            if first_child:
                shared_lot_batch_id = first_child.id
                register_seed(f"batch:{first_child.id}", first_child)
        # Register any newly split child batches from transfers
        for b in Batch.objects.filter(origin_batch__isnull=False):
            register_seed(f"batch:{b.id}", b)
        register_seed(tkey, trf)

    # Hold 3 batches (including 1 shared sibling batch from transfer #1!) for quality-control inspection demo
    if shared_lot_batch_id:
        hold_batch(actor=manager, batch_id=shared_lot_batch_id, reason="فحص جودة مشترك للدفعة المنقولة بين المحافظات")
    extra_hold = Batch.objects.filter(held_at__isnull=True, qty_left__gt=20, item_id__in=[items[75].id, items[76].id]).first()
    if extra_hold:
        hold_batch(actor=manager, batch_id=extra_hold.id, reason="مطابقة ملصق تاريخ الإنتاج مع المورّد")

    # ------------------------------------------------------------------
    # 4. Closed Stocktakes (4 historical across warehouses; open stocktake happens after orders!)
    # ------------------------------------------------------------------
    for sidx, (wh_idx, cat_name, d_ago) in enumerate(
        [(0, CATEGORIES[9][0], 56), (2, CATEGORIES[8][0], 42), (3, CATEGORIES[6][0], 28), (4, CATEGORIES[5][0], 14)],
        start=1,
    ):
        wh = warehouses[wh_idx]
        s_dt = _hist_dt(d_ago, hour=16)
        with frozen_now(s_dt):
            st_dict = create_stocktake(actor=keepers[wh_idx], data={"warehouse_id": wh.id, "category": cat_name})
            st = Stocktake.objects.prefetch_related("lines").get(id=st_dict["id"])
            upd_lines = []
            for lidx, sl in enumerate(st.lines.all()):
                # Introduce small realistic variance (-2 or +1) & 1 damaged piece on first 2 lines
                if lidx == 0 and sl.system_qty >= 5:
                    upd_lines.append({"item_id": sl.item_id, "counted_qty": sl.system_qty - 2, "damaged_qty": 1, "reason": "تلف تغليف أثناء الترصيف"})
                elif lidx == 1:
                    upd_lines.append({"item_id": sl.item_id, "counted_qty": sl.system_qty + 1, "damaged_qty": 0, "reason": "فرز كارتون إضافي"})
                else:
                    upd_lines.append({"item_id": sl.item_id, "counted_qty": sl.system_qty, "damaged_qty": 0, "reason": ""})
            update_stocktake(actor=keepers[wh_idx], stocktake_id=st.id, data={"lines": upd_lines})
            close_stocktake(actor=manager, stocktake_id=st.id)
        Stocktake.objects.filter(id=st.id).update(created_at=s_dt)
        register_seed(f"stk:closed:{sidx}", st)

    # ------------------------------------------------------------------
    # 5. Complete 90-Day Orders + Prepare + Shipments + Deliveries + Invoices Lifecycle
    # ------------------------------------------------------------------
    delivered_orders: list[Order] = []
    delivered_invoices: list[Invoice] = []
    out_orders: list[Order] = []
    shop_users = list(User.objects.filter(role=Role.SHOP, deleted=False).order_by("id"))

    # 5a: 86 historical CONFIRMED/DELIVERED orders over days -82..-1
    fast_items = items[:68]  # healthy stocked items across all categories 0..9
    for oidx in range(1, 87):
        days_ago = max(1, int(83 - (oidx * 0.94)))
        dt = _hist_dt(days_ago, hour=10 + (oidx % 6), minute=(oidx * 7) % 60)
        cust = active_customers[(oidx - 1) % len(active_customers)]
        # Primary warehouse matching geography, with occasional secondary WH split on prepare
        wh_idx = 0 if cust.city == "كربلاء" else (2 if cust.city == "النجف" else (3 if cust.city == "بغداد" else 4))
        wh = warehouses[wh_idx]
        creator = cust.rep or sales_reps[oidx % len(sales_reps)]
        # Make ~25% of orders placed directly by shop owner account if active shop matches
        if oidx % 4 == 0:
            ms = cust.memberships.filter(is_active_shop=True).select_related("user").first()
            if ms and ms.user.active:
                creator = ms.user

        allowed_cats = set(creator.rep_categories or []) if creator.role == Role.REP else set()
        pool = [it for it in fast_items if (not allowed_cats or it.category in allowed_cats)] or fast_items
        n_lines = 2 + (oidx % 3)
        chosen_items = [pool[(oidx * 3 + k * 7) % len(pool)] for k in range(n_lines)]
        # Deduplicate items within one order
        seen_ids: set[int] = set()
        line_specs = []
        for k, it in enumerate(chosen_items):
            if it.id in seen_ids:
                continue
            seen_ids.add(it.id)
            qty = 2 + ((oidx + k) % 4)
            line_specs.append({"item_id": it.id, "unit": "carton", "qty": qty})

        discount = 0
        if oidx % 9 == 0:
            discount = 12  # triggers needs_approval by manager!
        elif oidx % 5 == 0 and creator.role == Role.REP:
            discount = 2

        okey = f"ord:dlv:{oidx:03d}"
        with frozen_now(dt):
            ord_dict = create_order(
                actor=creator,
                data={
                    "customer_id": cust.id,
                    "warehouse_id": wh.id,
                    "discount_percent": discount,
                    "note": f"توريد دوري — {cust.name}",
                    "client_ref": f"seed-{okey}",
                    "lines": line_specs,
                    "custom": {
                        "cf_order_channel": "تطبيق المتجر" if creator.role == Role.SHOP else "زيارة ميدانية",
                    },
                },
            )
            order = Order.objects.get(id=ord_dict["id"])
            if order.status == OrderStatus.NEEDS_APPROVAL:
                approve_order(actor=manager, order_id=order.id)
            elif order.status == OrderStatus.PENDING:
                accept_order(actor=manager, order_id=order.id)

            order.refresh_from_db()
            # Prepare order (every 8th order mixes WH1 on line 1 + local hub on line 2 if WH != WH1)
            wmap: dict[str, int] = {}
            o_lines = list(order.lines.all())
            for l_i, oln in enumerate(o_lines):
                wmap[str(oln.id)] = warehouses[0].id if (oidx % 8 == 0 and l_i == 0 and wh.id != warehouses[0].id) else wh.id
            prepare_order(actor=manager, order_id=order.id, warehouses_map=wmap)

            # Plan shipment onto Fleet Vehicle + Load Shipment
            veh = vehicles[(oidx - 1) % len(vehicles)]
            load_lines = [{"item_id": ln.item_id, "unit": ln.unit, "qty": ln.qty} for ln in o_lines]
            plan_shipments(actor=manager, order_id=order.id, loads=[{"vehicle_id": veh.id, "lines": load_lines}])
            load_shipment(actor=manager, order_id=order.id)

            # Dispatch QR by assigned delivery driver (or round-robin fleet driver)
            order.refresh_from_db()
            dlv = order.delivery
            d_rep = (
                dlv.rep
                if (dlv.rep and "delivery" in (dlv.rep.rep_kinds or []))
                else delivery_reps[(oidx - 1) % len(delivery_reps)]
            )
            if dlv.rep_id and dlv.rep_id != d_rep.id:
                dlv.rep = d_rep
                dlv.save(update_fields=["rep"])
            dispatch_delivery(actor=d_rep, code=dlv.code)

            # Partial delivery on every 14th order with qty > 2
            deliv_lines_input = None
            rest_mode = "cancel"
            if oidx % 14 == 0 and o_lines[0].qty >= 3:
                deliv_lines_input = [
                    {"item_id": o_lines[0].item_id, "unit": o_lines[0].unit, "qty": o_lines[0].qty - 1},
                    *[{"item_id": ln.item_id, "unit": ln.unit, "qty": ln.qty} for ln in o_lines[1:]],
                ]
                rest_mode = "keep" if (oidx % 28 == 0) else "cancel"

            proof_meta = None
            proof_data_url = fallback_proof
            if with_media and (oidx <= 24 or oidx % 3 == 0):
                proof_meta = render_delivery_proof(seed_key=f"dlv:{order.id}", order_id=order.id, order_number=order.number)
                proof_data_url = bytes_to_data_url(proof_meta["_bytes"], "image/webp")

            dlv_dt = dt + timedelta(hours=3)
            with frozen_now(dlv_dt):
                deliver_order(
                    actor=d_rep,
                    order_id=order.id,
                    data={
                        "pin": decrypt_secret(dlv.pin_encrypted),
                        "photo": proof_data_url,
                        "rest": rest_mode,
                        **({"lines": deliv_lines_input} if deliv_lines_input else {}),
                    },
                )

        # Align Order/Invoice/Delivery timestamps cleanly onto historical timeline
        Order.objects.filter(id=order.id).update(created_at=dt, recorded_at=dt, accepted_at=dt + timedelta(minutes=20))
        Delivery.objects.filter(order_id=order.id).update(dispatched_at=dt + timedelta(hours=1), delivered_at=dlv_dt)
        inv = Invoice.objects.get(order_id=order.id)
        Invoice.objects.filter(id=inv.id).update(created_at=dlv_dt, delivered_at=dlv_dt)
        order.refresh_from_db()
        inv.refresh_from_db()
        if order.rest_order_id:
            register_raw_seed(f"{okey}:rest", app_label="orders", model_name="order", object_id=order.rest_order_id)
        if proof_meta:
            dlv.refresh_from_db()
            proof_meta["storage_key"] = dlv.photo_path
            proof_assets.append(proof_meta)
            register_seed(f"media:dlv:{order.id}", order, extra={"bucket": "delivery-proofs", "key": dlv.photo_path})

        register_seed(okey, order)
        register_seed(f"inv:{inv.id}", inv)
        delivered_orders.append(order)
        delivered_invoices.append(inv)

    # 5b: Live non-delivered orders (OUT x6, READY x6, PENDING x8, NEEDS_APPROVAL x6, WAITING_STOCK x4, CANCELLED x6)
    # Use tail end of days (today / 1-5 days ago) so dashboard live queues look crisp!
    for lidx in range(1, 37):
        cust = active_customers[(lidx * 2) % len(active_customers)]
        wh = warehouses[lidx % len(warehouses)]
        creator = cust.rep or sales_reps[lidx % len(sales_reps)]
        allowed_cats = set(creator.rep_categories or []) if creator.role == Role.REP else set()
        live_pool = [it for it in items[:45] if (not allowed_cats or it.category in allowed_cats)] or items[:45]
        item_a = live_pool[(lidx * 2) % len(live_pool)]
        item_b = live_pool[(lidx * 2 + 1) % len(live_pool)]
        okey = f"ord:live:{lidx:02d}"
        o_dt = _hist_dt(min(4, lidx % 5), hour=9 + (lidx % 4))

        with frozen_now(o_dt):
            if lidx <= 6:
                # OUT for delivery right now!
                od = create_order(
                    actor=creator,
                    data={
                        "customer_id": cust.id,
                        "warehouse_id": wh.id,
                        "client_ref": f"seed-{okey}",
                        "note": "شحنة جارية قيد التوصيل اليوم",
                        "lines": [{"item_id": item_a.id, "unit": "carton", "qty": 2}, {"item_id": item_b.id, "unit": "carton", "qty": 1}],
                    },
                )
                o_obj = Order.objects.get(id=od["id"])
                if o_obj.status == OrderStatus.NEEDS_APPROVAL:
                    approve_order(actor=manager, order_id=o_obj.id)
                elif o_obj.status == OrderStatus.PENDING:
                    accept_order(actor=manager, order_id=o_obj.id)
                o_obj.refresh_from_db()
                prepare_order(actor=manager, order_id=o_obj.id, warehouses_map={str(ln.id): wh.id for ln in o_obj.lines.all()})
                plan_shipments(
                    actor=manager,
                    order_id=o_obj.id,
                    loads=[{"vehicle_id": vehicles[lidx % len(vehicles)].id, "lines": [{"item_id": ln.item_id, "unit": ln.unit, "qty": ln.qty} for ln in o_obj.lines.all()]}],
                )
                load_shipment(actor=manager, order_id=o_obj.id)
                o_obj.refresh_from_db()
                dlv_live = o_obj.delivery
                d_rep = (
                    dlv_live.rep
                    if (dlv_live.rep and "delivery" in (dlv_live.rep.rep_kinds or []))
                    else delivery_reps[lidx % len(delivery_reps)]
                )
                if dlv_live.rep_id and dlv_live.rep_id != d_rep.id:
                    dlv_live.rep = d_rep
                    dlv_live.save(update_fields=["rep"])
                dispatch_delivery(actor=d_rep, code=dlv_live.code)
                if lidx == 6:
                    report_delivery_problem(
                        actor=d_rep,
                        data={"order_ids": [o_obj.id], "reason": "shop_closed", "note": "المحل مغلق وقت الظهيرة — سيعاد التوصيل عصراً"},
                    )
                out_orders.append(o_obj)
            elif lidx <= 12:
                # READY prepared on warehouse dock (3 planned on vehicles, 3 unplanned)
                od = create_order(
                    actor=creator,
                    data={
                        "customer_id": cust.id,
                        "warehouse_id": wh.id,
                        "client_ref": f"seed-{okey}",
                        "note": "جاهز للتحميل على خط التوزيع التالي",
                        "lines": [{"item_id": item_a.id, "unit": "carton", "qty": 2}],
                    },
                )
                o_obj = Order.objects.get(id=od["id"])
                if o_obj.status == OrderStatus.NEEDS_APPROVAL:
                    approve_order(actor=manager, order_id=o_obj.id)
                elif o_obj.status == OrderStatus.PENDING:
                    accept_order(actor=manager, order_id=o_obj.id)
                prepare_order(actor=manager, order_id=o_obj.id, warehouses_map={str(ln.id): wh.id for ln in o_obj.lines.all()})
                if lidx <= 9:
                    plan_shipments(
                        actor=manager,
                        order_id=o_obj.id,
                        loads=[{"vehicle_id": vehicles[lidx % len(vehicles)].id, "lines": [{"item_id": ln.item_id, "unit": ln.unit, "qty": ln.qty} for ln in o_obj.lines.all()]}],
                    )
            elif lidx <= 20:
                # PENDING acceptance queue
                od = create_order(
                    actor=shop_users[lidx % len(shop_users)] if lidx % 2 == 0 else creator,
                    data={
                        "customer_id": cust.id,
                        "warehouse_id": wh.id,
                        "client_ref": f"seed-{okey}",
                        "note": "طلب وارد بانتظار قبول المخزن",
                        "lines": [{"item_id": item_a.id, "unit": "carton", "qty": 2}, {"item_id": item_b.id, "unit": "piece", "qty": 6}],
                    },
                )
                o_obj = Order.objects.get(id=od["id"])
            elif lidx <= 26:
                # NEEDS_APPROVAL queue (high discount 14-18%)
                od = create_order(
                    actor=manager,
                    data={
                        "customer_id": cust.id,
                        "warehouse_id": wh.id,
                        "discount_percent": 14 + (lidx % 5),
                        "client_ref": f"seed-{okey}",
                        "note": "عرض ترويجي خاص يحتاج اعتماد المدير",
                        "lines": [{"item_id": item_a.id, "unit": "carton", "qty": 5}],
                    },
                )
                o_obj = Order.objects.get(id=od["id"])
            elif lidx <= 30:
                # WAITING_STOCK via offline=True rep sync with shortage on low-stock item[91] (use unrestricted rep & own customer)
                unrestricted_rep = next((r for r in sales_reps if not r.rep_categories), sales_reps[0])
                rep_custs = [c for c in active_customers if c.rep_id == unrestricted_rep.id] or [cust]
                off_cust = rep_custs[lidx % len(rep_custs)]
                od = create_order(
                    actor=unrestricted_rep,
                    data={
                        "customer_id": off_cust.id,
                        "warehouse_id": warehouses[0].id,
                        "offline": True,
                        "recorded_at": (o_dt - timedelta(hours=4)).isoformat(),
                        "client_ref": f"seed-{okey}",
                        "note": "مزامنة أوفلاين من المندوب — بانتظار توفر رصيد كامل",
                        "lines": [{"item_id": items[91].id, "unit": "piece", "qty": 80}],
                    },
                )
                o_obj = Order.objects.get(id=od["id"])
            else:
                # CANCELLED orders
                od = create_order(
                    actor=creator,
                    data={
                        "customer_id": cust.id,
                        "warehouse_id": wh.id,
                        "client_ref": f"seed-{okey}",
                        "lines": [{"item_id": item_a.id, "unit": "carton", "qty": 2}],
                    },
                )
                o_obj = Order.objects.get(id=od["id"])
                cancel_order(actor=manager, order_id=o_obj.id, reason="تعديل خطة الشراء من قِبل الزبون")
                o_obj.refresh_from_db()

        Order.objects.filter(id=o_obj.id).update(created_at=o_dt)
        register_seed(okey, o_obj)

    # ------------------------------------------------------------------
    # 6. Financials: Payments, Simulated Qi Card, Reversals, Returns, CreditNotes, Refunds
    # ------------------------------------------------------------------
    payments_created: list[Payment] = []
    # Collect FIFO payments for ~80% of delivered invoices (full + partial payments + some overpayments)
    for idx, inv in enumerate(delivered_invoices):
        # Leave last ~16 invoices open or partially paid so AR / Debts Aging dashboard is rich!
        if idx >= len(delivered_invoices) - 10:
            continue
        p_dt = inv.delivered_at + timedelta(days=1 + (idx % 6))
        if p_dt > baghdad_now().astimezone(UTC):
            p_dt = baghdad_now().astimezone(UTC) - timedelta(hours=2)
        actor_fin = accountants[idx % len(accountants)] if idx % 3 != 0 else (inv.rep or manager)
        # Verify permission: reps only receive payments for their own customers
        if actor_fin.role == Role.REP and inv.customer.rep_id != actor_fin.id:
            actor_fin = accountants[0]

        amt = inv.total
        if idx >= len(delivered_invoices) - 22:
            amt = max(250, (inv.total // 2 // 250) * 250)
        elif idx in (50, 52, 54, 56, 58, 60):
            # Overpay slightly on customer's last settled invoice so positive credit funds Refunds
            amt = inv.total + 150_000

        pkey = f"pay:{idx + 1:03d}"
        with frozen_now(p_dt):
            if idx in (5, 15, 25, 35, 45, 55):
                # Simulated settled Qi Card payment (`OnlinePayment` + real FIFO `Payment`)
                p_res = create_payment(
                    actor=accountants[0],
                    data={
                        "customer_id": inv.customer_id,
                        "amount": amt,
                        "method": PaymentMethod.TRANSFER,
                        "note": f"سداد إلكتروني Qi Card محاكى — مرجع QICARD-SIM-{idx + 1:04d}",
                        "client_ref": f"seed-{pkey}",
                    },
                )
                pay_obj = Payment.objects.get(id=p_res["id"])
                op, _ = OnlinePayment.objects.get_or_create(
                    request_id=f"REQ-SIM-{inv.id:05d}",
                    defaults={
                        "payment_id": f"QICARD-SIM-{inv.id:05d}",
                        "customer_id": inv.customer_id,
                        "initiated_by": accountants[0],
                        "amount": amt,
                        "status": OnlinePaymentStatus.SUCCESS,
                        "recorded_payment": pay_obj,
                        "form_url": "",
                        "return_url": "https://aln.ama8.uk/payments",
                        "client_ref": f"seed-onlpay-{inv.id}",
                        "raw_payload": {"simulated": True, "demo": True, "invoice_number": inv.number, "order_id": inv.order_id},
                    },
                )
                register_seed(f"onlpay:{idx + 1}", op)
            else:
                method = PaymentMethod.TRANSFER if idx % 4 == 0 else PaymentMethod.CASH
                p_res = create_payment(
                    actor=actor_fin,
                    data={
                        "customer_id": inv.customer_id,
                        "amount": amt,
                        "method": method,
                        "note": f"تسديد {'حوالة مصرفية' if method == PaymentMethod.TRANSFER else 'نقدي'} عن {inv.number}",
                        "client_ref": f"seed-{pkey}",
                    },
                )
                pay_obj = Payment.objects.get(id=p_res["id"])

        register_seed(pkey, pay_obj)
        payments_created.append(pay_obj)

    # Reverse 4 historical payments via real `reverse_payment`
    for r_i, pay_to_rev in enumerate(payments_created[8:12], start=1):
        rev_dt = _hist_dt(30 - r_i * 4, hour=15)
        with frozen_now(rev_dt):
            reverse_payment(actor=accountants[0], payment_id=pay_to_rev.id, reason="خطأ إدخال رقم الحوالة وإعادة القيد")

    # 8 Credit Notes on unpaid/partially-paid invoices (refresh from DB after FIFO payments)
    fresh_invoices = list(Invoice.objects.filter(id__in=[i.id for i in delivered_invoices]).order_by("-id"))
    cn_candidates = [inv for inv in fresh_invoices if inv.remaining >= 50_000][:8]
    for cn_i, inv in enumerate(cn_candidates, start=1):
        inv.refresh_from_db()
        if inv.remaining < 1_000:
            continue
        cn_amt = min(inv.remaining, 25_000 + cn_i * 5_000)
        cn_dt = inv.delivered_at + timedelta(days=2)
        if cn_dt > baghdad_now().astimezone(UTC):
            cn_dt = baghdad_now().astimezone(UTC) - timedelta(hours=1)
        with frozen_now(cn_dt):
            cn_res = create_credit_note(
                actor=accountants[0],
                invoice_id=inv.id,
                data={"amount": cn_amt, "reason": "تسوية فرق سعر حملة ترويجية وتلف طفيف بالكارتون الخارجي"},
            )
        register_raw_seed(f"cn:{cn_i:02d}", app_label="invoices", model_name="creditnote", object_id=cn_res["id"])

    # 12 Returns (good condition restocked + damaged logged)
    ret_candidates = [inv for inv in delivered_invoices if inv.lines.exists()][12:24]
    for ret_i, inv in enumerate(ret_candidates, start=1):
        first_line = inv.lines.order_by("id").first()
        if not first_line or (first_line.pieces - first_line.returned_pieces) < 2:
            continue
        r_dt = inv.delivered_at + timedelta(days=3)
        if r_dt > baghdad_now().astimezone(UTC):
            r_dt = baghdad_now().astimezone(UTC) - timedelta(hours=1)
        cond = "damaged" if ret_i % 3 == 0 else "good"
        with frozen_now(r_dt):
            ret_res = create_return(
                actor=manager,
                data={
                    "invoice_id": inv.id,
                    "note": "مرتجع بضاعة ضمن مدة السماح" if cond == "good" else "مرتجع تالف أثناء النقل والتنزيل",
                    "client_ref": f"seed-ret-{ret_i:02d}",
                    "lines": [{"item_id": first_line.item_id, "unit": "piece", "qty": min(2, first_line.pieces), "condition": cond}],
                },
            )
        ret_obj = ReturnDoc.objects.get(id=ret_res["id"])
        ReturnDoc.objects.filter(id=ret_obj.id).update(created_at=r_dt)
        register_seed(f"ret:{ret_i:02d}", ret_obj)

    # 6 Refunds for customers holding overpaid/returned credit balances
    from apps.customers.services import customer_balance_map

    bal_map = customer_balance_map([c.id for c in active_customers])
    refund_count = 0
    for cust in active_customers:
        if refund_count >= 6:
            break
        avail_credit = max(0, -bal_map.get(cust.id, 0))
        if avail_credit >= 10_000:
            refund_count += 1
            ref_amt = min(avail_credit, 50_000)
            rf_dt = _hist_dt(6 + refund_count * 2, hour=14)
            with frozen_now(rf_dt):
                rf_res = create_refund(
                    actor=accountants[0],
                    customer_id=cust.id,
                    data={
                        "amount": ref_amt,
                        "method": "cash",
                        "reason": "إرجاع فائض رصيد دائن للزبون بعد تسوية المرتجعات",
                    },
                )
            register_raw_seed(f"refund:{refund_count:02d}", app_label="payments", model_name="refund", object_id=rf_res["id"])

    # ------------------------------------------------------------------
    # 7. Close 2 Historical Commission Months + Leave Current Month Open
    # ------------------------------------------------------------------
    for m_back in (2, 1):
        m_str = _prev_month_str(today, m_back)
        if not ClosedCommissionMonth.objects.filter(month=m_str).exists():
            close_commission_month(actor=manager, month_raw=m_str)
            cm_obj = ClosedCommissionMonth.objects.get(month=m_str)
            register_seed(f"comm:{m_str}", cm_obj)

    # ------------------------------------------------------------------
    # 8. Expenses (28 operational expenses across categories, methods, warehouses, vehicles & statuses)
    # ------------------------------------------------------------------
    exp_cats = [
        ("fuel", "محطة وقود كربلاء الحديثة", 125_000),
        ("maintenance", "ورشة صيانة البرادات المركزية", 340_000),
        ("utilities", "دائرة كهرباء وخدمات المستودعات", 280_000),
        ("salaries", "سلفة رواتب كادر التحميل الأسبوعية", 850_000),
        ("rent", "إيجار ساحة وقوف الشاحنات الشهرية", 600_000),
        ("hospitality", "ضيافة وفود الموزعين والاجتماعات", 95_000),
        ("marketing", "طباعة بروشورات وحملات موسمية", 210_000),
    ]
    for ex_idx in range(1, 29):
        cat, paid_to, base_amt = exp_cats[(ex_idx - 1) % len(exp_cats)]
        amt = base_amt + (ex_idx % 4) * 25_000
        ex_date = (today - timedelta(days=min(80, ex_idx * 3))).isoformat()
        wh = warehouses[(ex_idx - 1) % len(warehouses)]
        veh = vehicles[(ex_idx - 1) % len(vehicles)] if cat in ("fuel", "maintenance") else None
        creator_u = accountants[0] if ex_idx % 2 == 0 else keepers[ex_idx % len(keepers)]
        bill_meta = None
        photo_url = None
        if with_media:
            bill_meta = render_expense_bill(
                seed_key=f"exp:{ex_idx:02d}",
                expense_id=ex_idx,
                number=f"EXP-SEED-{ex_idx:03d}",
                category=cat,
                amount=amt,
            )
            photo_url = bytes_to_data_url(bill_meta["_bytes"], "image/webp")

        e_dt = _hist_dt(min(80, ex_idx * 3), hour=13)
        with frozen_now(e_dt):
            ex_res = create_expense(
                actor=creator_u,
                data={
                    "amount": amt,
                    "category": cat,
                    "paid_to": f"{paid_to} #{ex_idx}",
                    "date": ex_date,
                    "method": "transfer" if ex_idx % 3 == 0 else "cash",
                    "warehouse_id": wh.id,
                    **({"vehicle_id": veh.id} if veh else {}),
                    "note": f"سند صرف تشغيلي دوري — {cat}",
                    **({"photo": photo_url} if photo_url else {}),
                },
            )
            ex_obj = Expense.objects.get(id=ex_res["id"])
            if ex_idx <= 18 and ex_obj.status == "pending":
                decide_expense(actor=manager, expense_id=ex_obj.id, ok=True)
            elif 19 <= ex_idx <= 21 and ex_obj.status == "pending":
                decide_expense(actor=manager, expense_id=ex_obj.id, ok=False, reason="بانتظار إرفاق الوصل الضريبي الأصلي")
            elif 22 <= ex_idx <= 23:
                cancel_expense(actor=creator_u, expense_id=ex_obj.id, reason="تكرار القيد مع حوالة البنك")
            # 24..28 remain in PENDING approval state!
        ex_obj.refresh_from_db()
        if bill_meta and ex_obj.photo_path:
            bill_meta["entity_id"] = ex_obj.id
            bill_meta["storage_key"] = ex_obj.photo_path
            expense_assets.append(bill_meta)
            register_seed(f"media:exp:{ex_obj.id}", ex_obj, extra={"bucket": "delivery-proofs", "key": ex_obj.photo_path})
        register_seed(f"exp:{ex_idx:02d}", ex_obj)

    # ------------------------------------------------------------------
    # 9. One Open Stocktake Now (narrow category in WH2 so main items remain unrestricted)
    # ------------------------------------------------------------------
    open_stk_dt = _hist_dt(0, hour=8)
    with frozen_now(open_stk_dt):
        stk_open_dict = create_stocktake(
            actor=keepers[1],
            data={"warehouse_id": warehouses[1].id, "category": CATEGORIES[9][0]},
        )
        register_raw_seed("stk:open:1", app_label="stocktakes", model_name="stocktake", object_id=stk_open_dict["id"])

    # ------------------------------------------------------------------
    # 10. Customer-Support Chat Threads (18 threads, ~110 messages, varied unread)
    # ------------------------------------------------------------------
    chat_templates = [
        ("السلام عليكم، نود التأكد من موعد وصول شحنة كربلاء الصباحية.", "وعليكم السلام ورحمة الله، الشحنة خرجت مع المندوب وستصل خلال ساعتين بإذن الله."),
        ("هل تتوفر عروض جملة جديدة على أرز بسمتي النهرين 5 كغم؟", "نعم، تم تفعيل خصم شريحة كبار العملاء 6% للكميات فوق 20 كارتون."),
        ("نرجو إرسال كشف الحساب المحدث لنهاية الأسبوع.", "تم إرسال كشف الحساب التفصيلي على المنصة ويمكنكم تحميله مباشرة من صفحة الكشوفات."),
        ("طلبنا تعديل سقف الدين من 5 إلى 10 مليون لتغطية تجهيزات الشهر.", "تم رفع الطلب للإدارة المالية والموافقة جارية اليوم."),
        ("أحد الكراتين كان مفتوحاً جزئياً عند الاستلام وسجلنا الملاحظة.", "شكراً لتنبيهكم، تم تسجيل إشعار تعويض على الفاتورة فوراً."),
        ("نرغب بإضافة فرعنا الجديد في حي الأمير بنفس الحساب.", "تم ربط المتجر الإضافي بحسابكم، يمكنكم التبديل بين المحلين من أعلى الشاشة."),
    ]
    for ch_i in range(1, 19):
        cust = active_customers[(ch_i - 1) % len(active_customers)]
        ms = cust.memberships.select_related("user").first()
        shop_actor = ms.user if ms and ms.user.active else None
        q_txt, a_txt = chat_templates[(ch_i - 1) % len(chat_templates)]
        for msg_pair in range(3):
            m_dt = _hist_dt(max(0, 18 - ch_i - msg_pair), hour=10 + msg_pair * 2)
            suffix = "" if msg_pair == 0 else f" (متابعة #{msg_pair + 1})"
            with frozen_now(m_dt):
                if shop_actor:
                    send_my_chat_message(actor=shop_actor, text=f"{q_txt}{suffix}")
                else:
                    send_customer_chat_message(actor=staff_users[ch_i % len(staff_users)], customer_id=cust.id, text=f"رسالة متابعة مبيعات: {q_txt}{suffix}")
                if not (ch_i >= 14 and msg_pair == 2):
                    # Leave last 5 threads with unread incoming messages for company inbox!
                    send_customer_chat_message(
                        actor=manager if ch_i % 2 == 0 else staff_users[ch_i % len(staff_users)],
                        customer_id=cust.id,
                        text=f"{a_txt}{suffix}",
                    )
        if ch_i <= 12:
            mark_chat_read(actor=manager, customer_id=cust.id)
            if shop_actor:
                mark_chat_read(actor=shop_actor, customer_id=cust.id)

    # Mark operations complete and flush outbox queue cleanly
    register_seed("ops:done", Order.objects.order_by("id").first())
    process_outbox_events_task(batch_size=200)

    return {
        "already_seeded": False,
        "supply_orders": len(supply_orders),
        "receipts": len(receipts),
        "delivered_orders": len(delivered_orders),
        "delivered_invoices": len(delivered_invoices),
        "out_orders": len(out_orders),
        "payments": len(payments_created),
        "proof_assets": proof_assets,
        "expense_assets": expense_assets,
    }
