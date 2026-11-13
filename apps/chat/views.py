"""DRF views for Support Chat endpoints."""
from __future__ import annotations

from rest_framework import status as http_status
from rest_framework.request import Request
from rest_framework.views import APIView

from apps.accounts.models import Role
from apps.accounts.permissions import role_permission
from apps.common.responses import created_response, ok_response

from .services import (
    get_my_chat,
    list_chat_threads,
    list_customer_chat,
    mark_chat_read,
    send_customer_chat_message,
    send_my_chat_message,
)


class ChatMeView(APIView):
    permission_classes = [role_permission(Role.SHOP)]

    def get(self, request: Request):
        return ok_response(data=get_my_chat(actor=request.user))

    def post(self, request: Request):
        data = send_my_chat_message(actor=request.user, text=str(request.data.get("text") or ""))
        return created_response(data=data)


class ChatThreadsView(APIView):
    permission_classes = [role_permission(Role.STAFF, Role.MANAGER)]

    def get(self, request: Request):
        return ok_response(data=list_chat_threads(actor=request.user))


class ChatCustomerThreadView(APIView):
    permission_classes = [role_permission(Role.STAFF, Role.MANAGER)]

    def get(self, request: Request, customer_id: int):
        return ok_response(data=list_customer_chat(actor=request.user, customer_id=customer_id))

    def post(self, request: Request, customer_id: int):
        data = send_customer_chat_message(
            actor=request.user,
            customer_id=customer_id,
            text=str(request.data.get("text") or ""),
        )
        return created_response(data=data)


class ChatMarkReadView(APIView):
    permission_classes = [role_permission(Role.SHOP, Role.STAFF, Role.MANAGER)]

    def post(self, request: Request, customer_id: int):
        return ok_response(
            data=mark_chat_read(actor=request.user, customer_id=customer_id),
            status=http_status.HTTP_200_OK,
        )
