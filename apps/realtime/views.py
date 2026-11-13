"""DRF views for short-lived WebSocket handshake ticket issuance."""
from __future__ import annotations

from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.views import APIView

from apps.accounts.permissions import get_active_customer_id
from apps.common.responses import ok_response
from apps.common.utils import client_ip
from apps.realtime.tickets import issue_realtime_ticket


class RealtimeTicketView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request: Request):
        sid = ""
        if request.auth is not None and hasattr(request.auth, "get"):
            sid = str(request.auth.get("sid") or "")
        active_cid = get_active_customer_id(request.user)
        ticket, ttl = issue_realtime_ticket(
            user=request.user,
            session_id=sid,
            active_customer_id=active_cid,
            ip_address=client_ip(request),
        )
        return ok_response(data={"ticket": ticket, "expires_in": ttl})
