"""End-to-end Django Channels WebSocket tests: ticket auth, security limits, ACLs, GPS, and on_commit fanout."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from asgiref.sync import sync_to_async
from channels.testing import WebsocketCommunicator
from django.core.cache import cache
from django.test import override_settings
from rest_framework.test import APIClient

from apps.accounts.models import PriceTier, RepKind, Role, SystemSetting, User, UserSession
from apps.common.utils import baghdad_today
from apps.customers.models import Customer, CustomerStatus, ShopMembership
from apps.deliveries.models import Delivery, RepLocation
from apps.realtime.models import RealtimeTicket
from apps.warehouses.models import Warehouse
from config.asgi import application


def _auth_client_and_sid(phone: str) -> tuple[APIClient, str]:
    cli = APIClient()
    res = cli.post("/api/v1/auth/login", {"phone": phone, "password": "Password123"}, format="json")
    assert res.status_code == 200
    tok = res.json()["data"]["token"]
    cli.credentials(HTTP_AUTHORIZATION=f"Bearer {tok}")
    sess = UserSession.objects.filter(user__phone=phone).order_by("-id").first()
    return cli, sess.session_id


def _mint_ticket(cli: APIClient) -> str:
    res = cli.post("/api/v1/realtime/ticket", {}, format="json")
    assert res.status_code == 200
    data = res.json()["data"]
    assert data["expires_in"] >= 5
    return data["ticket"]


def _seed_realtime_world():
    cache.clear()
    wh1 = Warehouse.objects.create(name="مخزن ١", name_en="WH1", code="RTW1", active=True)
    wh2 = Warehouse.objects.create(name="مخزن ٢", name_en="WH2", code="RTW2", active=True)
    tier = PriceTier.objects.create(name="جملة", name_en="Wholesale", sort_order=1)
    SystemSetting.get_solo()

    mgr = User.objects.create_user(phone="07730000001", password="Password123", name="Mgr", role=Role.MANAGER)
    staff = User.objects.create_user(
        phone="07730000002", password="Password123", name="Staff", role=Role.STAFF, warehouse=wh1
    )
    k1 = User.objects.create_user(
        phone="07730000003", password="Password123", name="K1", role=Role.WAREHOUSE, warehouse=wh1
    )
    k2 = User.objects.create_user(
        phone="07730000004", password="Password123", name="K2", role=Role.WAREHOUSE, warehouse=wh2
    )
    rep = User.objects.create_user(
        phone="07730000005",
        password="Password123",
        name="Rep",
        role=Role.REP,
        rep_kinds=[RepKind.SALES, RepKind.DELIVERY],
    )
    shop1_u = User.objects.create_user(phone="07730000010", password="Password123", name="Shop1", role=Role.SHOP)
    shop2_u = User.objects.create_user(phone="07730000011", password="Password123", name="Shop2", role=Role.SHOP)

    c1 = Customer.objects.create(
        name="محل ١",
        owner_name="A",
        phone="07730000010",
        city="بغداد",
        area="الكرادة",
        street="1",
        landmark="A",
        tier=tier,
        rep=rep,
        debt_limit=500000,
        status=CustomerStatus.ACTIVE,
        active=True,
    )
    c2 = Customer.objects.create(
        name="محل ٢",
        owner_name="B",
        phone="07730000011",
        city="بغداد",
        area="المنصور",
        street="2",
        landmark="B",
        tier=tier,
        debt_limit=500000,
        status=CustomerStatus.ACTIVE,
        active=True,
    )
    ShopMembership.objects.create(user=shop1_u, customer=c1, is_primary=True, is_active_shop=True)
    ShopMembership.objects.create(user=shop2_u, customer=c2, is_primary=True, is_active_shop=True)

    mgr_cli, _ = _auth_client_and_sid("07730000001")
    it = mgr_cli.post(
        "/api/v1/items",
        {
            "name": "شاي",
            "name_en": "Tea",
            "category": "مشروبات",
            "pieces_per_carton": 10,
            "prices": [{"tier_id": tier.id, "carton_price": 10000, "piece_price": 1000}],
        },
        format="json",
    ).json()["data"]["id"]
    k1_cli, _ = _auth_client_and_sid("07730000003")
    k1_cli.post(
        "/api/v1/receipts",
        {
            "warehouse_id": wh1.id,
            "lines": [
                {
                    "item_id": it,
                    "unit": "carton",
                    "qty": 10,
                    "expiry_date": (baghdad_today() + timedelta(days=90)).isoformat(),
                    "purchase_price": 600,
                }
            ],
        },
        format="json",
    )
    return {
        "wh1": wh1,
        "wh2": wh2,
        "mgr": mgr,
        "staff": staff,
        "k1": k1,
        "k2": k2,
        "rep": rep,
        "c1": c1,
        "c2": c2,
        "it": it,
    }


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_ws_ticket_security_replay_expiry_revocation_origin_cap_and_limits():
    await sync_to_async(_seed_realtime_world)()

    # 1. Unauthenticated ticket request -> 401
    def _unauth_ticket() -> int:
        return APIClient().post("/api/v1/realtime/ticket", {}, format="json").status_code

    assert await sync_to_async(_unauth_ticket)() == 401

    mgr_cli, mgr_sid = await sync_to_async(_auth_client_and_sid)("07730000001")

    # 2. Missing / bogus ticket rejected with 4001
    comm_bad = WebsocketCommunicator(application, "/ws?ticket=bogus-invalid-ticket")
    ok, code = await comm_bad.connect()
    assert not ok and code == 4001

    # 3. Valid single-use ticket connects; replay with same ticket fails with 4001
    t1 = await sync_to_async(_mint_ticket)(mgr_cli)
    comm1 = WebsocketCommunicator(application, f"/ws?ticket={t1}")
    ok, _ = await comm1.connect()
    assert ok

    # Ping -> pong
    await comm1.send_json_to({"action": "ping"})
    pong = await comm1.receive_json_from()
    assert pong["type"] == "pong"

    replay = WebsocketCommunicator(application, f"/ws?ticket={t1}")
    ok_r, code_r = await replay.connect()
    assert not ok_r and code_r == 4001

    # 4. Expired ticket rejected with 4001
    t_exp = await sync_to_async(_mint_ticket)(mgr_cli)
    await sync_to_async(
        lambda: RealtimeTicket.objects.filter(used_at__isnull=True).update(expires_at=datetime.now(tz=UTC) - timedelta(seconds=10))
    )()
    comm_exp = WebsocketCommunicator(application, f"/ws?ticket={t_exp}")
    ok_e, code_e = await comm_exp.connect()
    assert not ok_e and code_e == 4001

    # 5. Revoked session via /auth/logout rejected with 4001
    t_rev = await sync_to_async(_mint_ticket)(mgr_cli)
    logout_res = await sync_to_async(lambda: mgr_cli.post("/api/v1/auth/logout", {}, format="json"))()
    assert logout_res.status_code == 200
    comm_rev = WebsocketCommunicator(application, f"/ws?ticket={t_rev}")
    ok_v, code_v = await comm_rev.connect()
    assert not ok_v and code_v == 4001

    await comm1.disconnect()

    # Re-login fresh session for cap / origin / limits checks
    mgr_cli2, _ = await sync_to_async(_auth_client_and_sid)("07730000001")

    # 6. Disallowed Origin rejected with 4003; regex-matched CORS_ALLOWED_ORIGIN_REGEXES accepted
    with override_settings(
        DEBUG=False,
        CORS_ALLOW_ALL_ORIGINS=False,
        CORS_ALLOWED_ORIGINS=["https://app.example.com"],
        CSRF_TRUSTED_ORIGINS=[],
        CORS_ALLOWED_ORIGIN_REGEXES=[r"^https://.*\.ama8\.uk$"],
    ):
        t_orig = await sync_to_async(_mint_ticket)(mgr_cli2)
        evil = WebsocketCommunicator(application, f"/ws?ticket={t_orig}", headers=[(b"origin", b"https://evil.example")])
        ok_o, code_o = await evil.connect()
        assert not ok_o and code_o == 4003

        good_rx = WebsocketCommunicator(application, f"/ws?ticket={t_orig}", headers=[(b"origin", b"https://aln.ama8.uk")])
        ok_rx, _ = await good_rx.connect()
        assert ok_rx
        await good_rx.disconnect()

    # 7. Max connections per user cap (close 4008) & slot release on disconnect
    await sync_to_async(cache.clear)()
    with override_settings(REALTIME_MAX_CONNECTIONS_PER_USER=2):
        ta = await sync_to_async(_mint_ticket)(mgr_cli2)
        tb = await sync_to_async(_mint_ticket)(mgr_cli2)
        tc = await sync_to_async(_mint_ticket)(mgr_cli2)
        ca = WebsocketCommunicator(application, f"/ws?ticket={ta}")
        cb = WebsocketCommunicator(application, f"/ws?ticket={tb}")
        cc = WebsocketCommunicator(application, f"/ws?ticket={tc}")
        assert (await ca.connect())[0]
        assert (await cb.connect())[0]
        ok_c, code_c = await cc.connect()
        assert not ok_c and code_c == 4008
        await ca.disconnect()
        await cb.disconnect()

    # 8. Oversized message closes with 4009; rate-limit emits RATE_LIMITED
    with override_settings(REALTIME_MAX_MESSAGE_BYTES=64, REALTIME_RATE_LIMIT_PER_10S=2):
        td = await sync_to_async(_mint_ticket)(mgr_cli2)
        cd = WebsocketCommunicator(application, f"/ws?ticket={td}")
        assert (await cd.connect())[0]

        await cd.send_json_to({"action": "ping"})
        assert (await cd.receive_json_from())["type"] == "pong"
        await cd.send_json_to({"action": "ping"})
        assert (await cd.receive_json_from())["type"] == "pong"
        # 3rd message within 10s hits rate limit
        await cd.send_json_to({"action": "ping"})
        err_rl = await cd.receive_json_from()
        assert err_rl == {"type": "error", "code": "RATE_LIMITED"}

        # Oversized payload (> 64 bytes) closes with 4009
        await cd.send_to(text_data='{"action":"ping","pad":"' + ("x" * 100) + '"}')
        closed = await cd.receive_output(timeout=2)
        assert closed["type"] == "websocket.close" and closed["code"] == 4009


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_ws_topic_acls_http_on_commit_broadcasts_and_gps_fanout():
    data = await sync_to_async(_seed_realtime_world)()

    mgr_cli, _ = await sync_to_async(_auth_client_and_sid)("07730000001")
    staff_cli, _ = await sync_to_async(_auth_client_and_sid)("07730000002")
    k1_cli, _ = await sync_to_async(_auth_client_and_sid)("07730000003")
    k2_cli, _ = await sync_to_async(_auth_client_and_sid)("07730000004")
    rep_cli, _ = await sync_to_async(_auth_client_and_sid)("07730000005")
    shop1_cli, _ = await sync_to_async(_auth_client_and_sid)("07730000010")
    shop2_cli, _ = await sync_to_async(_auth_client_and_sid)("07730000011")

    ws_mgr = WebsocketCommunicator(application, f"/ws?ticket={await sync_to_async(_mint_ticket)(mgr_cli)}")
    ws_staff = WebsocketCommunicator(application, f"/ws?ticket={await sync_to_async(_mint_ticket)(staff_cli)}")
    ws_k2 = WebsocketCommunicator(application, f"/ws?ticket={await sync_to_async(_mint_ticket)(k2_cli)}")
    ws_rep = WebsocketCommunicator(application, f"/ws?ticket={await sync_to_async(_mint_ticket)(rep_cli)}")
    ws_shop1 = WebsocketCommunicator(application, f"/ws?ticket={await sync_to_async(_mint_ticket)(shop1_cli)}")
    ws_shop2 = WebsocketCommunicator(application, f"/ws?ticket={await sync_to_async(_mint_ticket)(shop2_cli)}")

    for ws in (ws_mgr, ws_staff, ws_k2, ws_rep, ws_shop1, ws_shop2):
        ok, _ = await ws.connect()
        assert ok

    # Subscribe with ACL checks
    await ws_mgr.send_json_to({"action": "subscribe", "topics": ["orders", "chat", "location", "notifications"]})
    await ws_staff.send_json_to({"action": "subscribe", "topics": ["chat", "notifications"]})
    await ws_k2.send_json_to({"action": "subscribe", "topics": ["orders", "chat"]})  # k2 forbidden from chat!
    assert (await ws_k2.receive_json_from()) == {"type": "error", "code": "FORBIDDEN", "topic": "chat"}
    await ws_rep.send_json_to({"action": "subscribe", "topics": ["chat"]})  # rep forbidden from chat!
    assert (await ws_rep.receive_json_from()) == {"type": "error", "code": "FORBIDDEN", "topic": "chat"}
    await ws_shop1.send_json_to({"action": "subscribe", "topics": ["orders", "chat", "location"]})
    await ws_shop2.send_json_to({"action": "subscribe", "topics": ["orders", "chat", "location"]})

    # Non-rep cannot publish location via WS
    await ws_shop1.send_json_to({"action": "publish", "topic": "location", "data": {"lat": 33.3, "lng": 44.3}})
    assert (await ws_shop1.receive_json_from()) == {"type": "error", "code": "FORBIDDEN"}

    # Let subscription group_add complete cleanly
    await asyncio.sleep(0.05)

    # 1. Support chat HTTP mutation -> broadcasts to staff/manager & ONLY owning shop1 (NOT shop2)
    def _send_shop1_chat():
        res = shop1_cli.post("/api/v1/chat/me", {"text": "متى يوصل الطلب؟"}, format="json")
        assert res.status_code == 201

    await sync_to_async(_send_shop1_chat)()
    staff_evs = [await ws_staff.receive_json_from(timeout=2)]
    for _ in range(4):
        if await ws_staff.receive_nothing(timeout=0.05):
            break
        staff_evs.append(await ws_staff.receive_json_from(timeout=0.5))
    assert any(ev["topic"] == "chat" and ev["data"]["customer_id"] == data["c1"].id for ev in staff_evs)
    assert any(ev["topic"] == "notifications" for ev in staff_evs)
    assert await ws_shop2.receive_nothing(timeout=0.1)

    # 2. Order creation + prepare + dispatch -> broadcasts minimal {"id"} wakeup (no pin/cost) to mgr & shop1, NOT k2 or shop2
    def _create_prepare_dispatch_order():
        ord_res = mgr_cli.post(
            "/api/v1/orders",
            {
                "customer_id": data["c1"].id,
                "warehouse_id": data["wh1"].id,
                "lines": [{"item_id": data["it"], "unit": "carton", "qty": 1}],
            },
            format="json",
        )
        assert ord_res.status_code == 201
        oid = ord_res.json()["data"]["id"]
        k1_cli.post(f"/api/v1/orders/{oid}/prepare", {"warehouses": {}}, format="json")
        dlv = Delivery.objects.get(order_id=oid)
        rep_cli.post("/api/v1/deliveries/dispatch", {"code": f"NAHRAIN:DLV:{dlv.code}"}, format="json")
        return oid

    oid = await sync_to_async(_create_prepare_dispatch_order)()
    mgr_ev = await ws_mgr.receive_json_from(timeout=2)
    shop1_ev = await ws_shop1.receive_json_from(timeout=2)
    assert mgr_ev["topic"] in ("orders", "notifications", "chat")
    # Drain queued mgr/shop1 events and ensure at least one orders.changed with no PIN leaked
    all_shop1 = [shop1_ev]
    for _ in range(5):
        if await ws_shop1.receive_nothing(timeout=0.05):
            break
        all_shop1.append(await ws_shop1.receive_json_from(timeout=0.5))
    order_evs = [e for e in all_shop1 if e.get("topic") == "orders"]
    assert any(e["data"] == {"id": oid} for e in order_evs)
    assert all("pin" not in e["data"] and "cost" not in e["data"] for e in order_evs)
    # WH2 keeper and unrelated shop2 received NOTHING
    assert await ws_k2.receive_nothing(timeout=0.1)
    assert await ws_shop2.receive_nothing(timeout=0.1)

    # Drain any notifications/events on mgr before checking GPS fanout
    for _ in range(10):
        if await ws_mgr.receive_nothing(timeout=0.05):
            break
        await ws_mgr.receive_json_from(timeout=0.5)

    # 3. Rep publishes live GPS via WS -> persists RepLocation & fans out to mgr and active-delivery shop1 ONLY
    await ws_rep.send_json_to({"action": "publish", "topic": "location", "data": {"lat": 33.3152, "lng": 44.3661}})
    loc_mgr = await ws_mgr.receive_json_from(timeout=2)
    loc_shop1 = await ws_shop1.receive_json_from(timeout=2)
    assert loc_mgr["topic"] == "location" and loc_mgr["type"] == "moved"
    assert loc_mgr["data"]["rep_id"] == data["rep"].id and loc_mgr["data"]["lat"] == 33.3152
    assert loc_shop1["topic"] == "location" and loc_shop1["data"]["rep_id"] == data["rep"].id
    assert await ws_shop2.receive_nothing(timeout=0.1)

    rep_loc = await sync_to_async(lambda: RepLocation.objects.get(rep=data["rep"]))()
    assert float(rep_loc.lat) == pytest.approx(33.3152, abs=1e-4)

    for ws in (ws_mgr, ws_staff, ws_k2, ws_rep, ws_shop1, ws_shop2):
        await ws.disconnect()
