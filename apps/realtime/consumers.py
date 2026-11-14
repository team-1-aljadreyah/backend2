"""Django Channels WebSocket consumer with ticket auth, origin guard, rate/size limits, and topic ACLs."""
from __future__ import annotations

import json
import re
import time
from collections import deque
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlparse

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer
from django.conf import settings
from django.core.cache import cache

from apps.accounts.models import Role, User
from apps.accounts.permissions import covering_warehouse_ids, get_active_customer_id
from apps.common.exceptions import ApiException
from apps.deliveries.services import record_rep_location
from apps.realtime.broadcaster import location_groups_for_rep
from apps.realtime.tickets import consume_realtime_ticket

_ALLOWED_TOPICS = {"notifications", "chat", "orders", "location"}
_CHAT_STAFF_ROLES = {Role.STAFF, Role.MANAGER}


def _user_profile_context(user: User) -> tuple[bool, list[int]]:
    """Return (hours_ok, covering_warehouse_ids) cleanly inside sync DB context."""
    from apps.accounts.profiles import in_hours, profile_of

    if getattr(user, "deleted", False) or not getattr(user, "active", False):
        return False, []
    prof = profile_of(user)
    if prof and not in_hours(prof.get("hours")):
        return False, []
    return True, covering_warehouse_ids(user)


def _authenticate_ws_connection(raw_ticket: str, raw_token: str) -> tuple[User, int | None, list[int]] | None:
    """Authenticate a WebSocket handshake via single-use ?ticket= OR Bearer access JWT ?token=."""
    if raw_ticket:
        ticket = consume_realtime_ticket(raw_ticket)
        if ticket is None:
            return None
        ok, cov = _user_profile_context(ticket.user)
        if not ok:
            return None
        return ticket.user, ticket.active_customer_id, cov

    if not raw_token:
        return None

    from apps.accounts.authentication import SessionAwareJWTAuthentication

    auth = SessionAwareJWTAuthentication()
    try:
        validated = auth.get_validated_token(raw_token)
        user = auth.get_user(validated)
    except Exception:
        return None

    ok, cov = _user_profile_context(user)
    if not ok:
        return None
    active_cid = getattr(user, "_session_customer_id", None)
    if user.role == Role.SHOP and active_cid is None:
        active_cid = get_active_customer_id(user)
    return user, active_cid if user.role == Role.SHOP else None, cov


def _now_iso() -> str:
    return datetime.now(tz=UTC).isoformat()


def _conn_key(user_id: int) -> str:
    return f"rt:ws:conns:{user_id}"


def _acquire_conn_slot(user_id: int, limit: int) -> bool:
    key = _conn_key(user_id)
    cache.add(key, 0, timeout=3600)
    try:
        current = cache.incr(key)
    except ValueError:
        cache.set(key, 1, timeout=3600)
        return True
    if current > limit:
        try:
            cache.decr(key)
        except ValueError:
            pass
        return False
    return True


def _release_conn_slot(user_id: int) -> None:
    key = _conn_key(user_id)
    try:
        new_val = cache.decr(key)
        if new_val <= 0:
            cache.delete(key)
    except ValueError:
        cache.delete(key)


@database_sync_to_async
def _save_rep_location_and_groups(user: User, lat: Any, lng: Any) -> tuple[dict[str, Any] | None, set[str], str | None]:
    try:
        loc_dict, should_broadcast = record_rep_location(
            actor=user,
            lat=lat,
            lng=lng,
            emit_realtime=False,
        )
    except ApiException as exc:
        return None, set(), exc.code
    groups = location_groups_for_rep(user.id) if should_broadcast else set()
    return loc_dict, groups, None


def _origin_allowed(scope: dict[str, Any]) -> bool:
    headers = dict(scope.get("headers", []))
    raw_origin = headers.get(b"origin", b"").decode("latin1").strip()
    if not raw_origin:
        return True
    if getattr(settings, "CORS_ALLOW_ALL_ORIGINS", False):
        return True

    allowed = set(getattr(settings, "CORS_ALLOWED_ORIGINS", [])) | set(
        getattr(settings, "CSRF_TRUSTED_ORIGINS", [])
    )
    if raw_origin in allowed:
        return True

    for pattern in getattr(settings, "CORS_ALLOWED_ORIGIN_REGEXES", []) or []:
        if isinstance(pattern, re.Pattern):
            if pattern.match(raw_origin):
                return True
        elif isinstance(pattern, str) and pattern:
            if re.match(pattern, raw_origin):
                return True

    parsed = urlparse(raw_origin)
    host_hdr = headers.get(b"host", b"").decode("latin1").strip()
    if host_hdr and parsed.netloc == host_hdr:
        return True

    if getattr(settings, "DEBUG", False) and parsed.hostname in {"localhost", "127.0.0.1"}:
        return True

    return False


def _groups_for_topic(
    user: User,
    active_customer_id: int | None,
    topic: str,
    covering_wh_ids: list[int] | None = None,
) -> list[str] | None:
    """Return authorized channel groups for (user, topic) or None if forbidden."""
    if topic == "notifications":
        return [f"user_{user.id}", f"role_{user.role}"]

    if topic == "chat":
        if user.role in _CHAT_STAFF_ROLES:
            return ["chat_staff"]
        if user.role == Role.SHOP and active_customer_id:
            return [f"shop_{active_customer_id}"]
        return None

    if topic == "orders":
        if user.role in (Role.MANAGER, Role.ACCOUNTANT):
            return [f"role_{user.role}"]
        if user.role in (Role.WAREHOUSE, Role.STAFF):
            wh_ids = list(dict.fromkeys(w for w in [user.warehouse_id, *(covering_wh_ids or [])] if w))
            return [f"wh_{wid}" for wid in wh_ids]
        if user.role == Role.REP:
            return [f"user_{user.id}"]
        if user.role == Role.SHOP:
            return [f"shop_{active_customer_id}"] if active_customer_id else []
        return None

    if topic == "location":
        if user.role in (Role.MANAGER, Role.ACCOUNTANT, Role.WAREHOUSE, Role.STAFF):
            return ["loc_staff"]
        if user.role == Role.REP:
            return [f"user_{user.id}"]
        if user.role == Role.SHOP:
            return [f"shop_{active_customer_id}"] if active_customer_id else []
        return None

    return None


class RealtimeConsumer(AsyncJsonWebsocketConsumer):
    user: User | None = None
    active_customer_id: int | None = None
    _covering_wh_ids: list[int]
    _conn_acquired: bool = False
    _joined_groups: set[str]
    _topic_groups: dict[str, list[str]]
    _msg_timestamps: deque[float]

    async def connect(self) -> None:
        self._joined_groups = set()
        self._topic_groups = {}
        self._msg_timestamps = deque()
        self._covering_wh_ids = []
        self._conn_acquired = False

        if not _origin_allowed(self.scope):
            await self.close(code=4003)
            return

        qs = parse_qs(self.scope.get("query_string", b"").decode("utf-8", errors="ignore"))
        raw_ticket = (qs.get("ticket") or [""])[0]
        raw_token = (qs.get("token") or [""])[0]
        auth_res = await database_sync_to_async(_authenticate_ws_connection)(raw_ticket, raw_token)
        if auth_res is None:
            await self.close(code=4001)
            return

        user, active_cid, cov_whs = auth_res
        max_conns = max(1, int(getattr(settings, "REALTIME_MAX_CONNECTIONS_PER_USER", 5)))
        if not await database_sync_to_async(_acquire_conn_slot)(user.id, max_conns):
            await self.close(code=4008)
            return

        self.user = user
        self.active_customer_id = active_cid
        self._covering_wh_ids = cov_whs
        self._conn_acquired = True
        await self.accept()

    async def disconnect(self, close_code: int) -> None:
        for grp in list(self._joined_groups):
            try:
                await self.channel_layer.group_discard(grp, self.channel_name)
            except Exception:
                pass
        self._joined_groups.clear()
        self._topic_groups.clear()

        if self._conn_acquired and self.user is not None:
            await database_sync_to_async(_release_conn_slot)(self.user.id)
            self._conn_acquired = False

    async def receive(self, text_data: str | None = None, bytes_data: bytes | None = None, **kwargs: Any) -> None:
        if bytes_data is not None or text_data is None:
            await self.close(code=4009)
            return

        max_bytes = int(getattr(settings, "REALTIME_MAX_MESSAGE_BYTES", 16384))
        if len(text_data.encode("utf-8")) > max_bytes:
            await self.close(code=4009)
            return

        now_mono = time.monotonic()
        cutoff = now_mono - 10.0
        while self._msg_timestamps and self._msg_timestamps[0] < cutoff:
            self._msg_timestamps.popleft()

        rate_limit = max(1, int(getattr(settings, "REALTIME_RATE_LIMIT_PER_10S", 30)))
        if len(self._msg_timestamps) >= rate_limit:
            await self.send_json({"type": "error", "code": "RATE_LIMITED"})
            if len(self._msg_timestamps) >= rate_limit * 2:
                await self.close(code=4029)
            else:
                self._msg_timestamps.append(now_mono)
            return
        self._msg_timestamps.append(now_mono)

        try:
            content = json.loads(text_data)
        except json.JSONDecodeError:
            await self.send_json({"type": "error", "code": "INVALID_JSON"})
            return

        if not isinstance(content, dict):
            await self.send_json({"type": "error", "code": "INVALID_PAYLOAD"})
            return

        await self.receive_json(content)

    async def receive_json(self, content: dict[str, Any], **kwargs: Any) -> None:
        if self.user is None:
            await self.close(code=4001)
            return

        action = str(content.get("action") or "").strip().lower()
        if action == "ping":
            await self.send_json({"type": "pong", "at": _now_iso()})
            return

        if action == "subscribe":
            raw_topics = content.get("topics")
            if not isinstance(raw_topics, list):
                await self.send_json({"type": "error", "code": "VALIDATION"})
                return
            for topic in raw_topics:
                t = str(topic or "").strip().lower()
                if t not in _ALLOWED_TOPICS:
                    await self.send_json({"type": "error", "code": "UNKNOWN_TOPIC", "topic": t})
                    continue
                groups = _groups_for_topic(
                    self.user,
                    self.active_customer_id,
                    t,
                    covering_wh_ids=self._covering_wh_ids,
                )
                if groups is None:
                    await self.send_json({"type": "error", "code": "FORBIDDEN", "topic": t})
                    continue
                self._topic_groups[t] = groups
                for grp in groups:
                    if grp not in self._joined_groups:
                        await self.channel_layer.group_add(grp, self.channel_name)
                        self._joined_groups.add(grp)
            return

        if action == "unsubscribe":
            raw_topics = content.get("topics")
            if not isinstance(raw_topics, list):
                return
            for topic in raw_topics:
                t = str(topic or "").strip().lower()
                removed = self._topic_groups.pop(t, [])
                still_needed = {g for gs in self._topic_groups.values() for g in gs}
                for grp in removed:
                    if grp not in still_needed and grp in self._joined_groups:
                        await self.channel_layer.group_discard(grp, self.channel_name)
                        self._joined_groups.discard(grp)
            return

        if action == "publish":
            topic = str(content.get("topic") or "").strip().lower()
            if topic != "location" or self.user.role != Role.REP:
                await self.send_json({"type": "error", "code": "FORBIDDEN"})
                return
            data = content.get("data")
            if not isinstance(data, dict):
                await self.send_json({"type": "error", "code": "VALIDATION"})
                return

            loc_dict, groups, err_code = await _save_rep_location_and_groups(
                self.user,
                data.get("lat"),
                data.get("lng"),
            )
            if err_code or loc_dict is None:
                await self.send_json({"type": "error", "code": err_code or "VALIDATION"})
                return
            if groups:
                payload = {
                    "topic": "location",
                    "type": "moved",
                    "data": loc_dict,
                    "at": loc_dict["at"],
                }
                for grp in groups:
                    await self.channel_layer.group_send(grp, {"type": "rt.event", "payload": payload})
            return

        await self.send_json({"type": "error", "code": "UNKNOWN_ACTION"})

    async def rt_event(self, event: dict[str, Any]) -> None:
        await self.send_json(event["payload"])
