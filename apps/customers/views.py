"""HTTP views for customers, special prices, multi-shop switching, and shop lists."""
from __future__ import annotations

from drf_spectacular.utils import extend_schema
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from apps.accounts.models import Role
from apps.accounts.permissions import role_permission
from apps.common.responses import created_response, ok_response
from apps.customers.serializers import (
    CustomerInputSerializer,
    CustomerSerializer,
    MyShopSerializer,
    MyShopSwitchInputSerializer,
    ShopListInputSerializer,
    ShopListSerializer,
)
from apps.customers.services import (
    create_additional_shop,
    create_customer,
    delete_shop_list,
    get_customer_for_actor,
    list_customers_for_actor,
    list_my_shops,
    list_shop_lists,
    list_special_prices,
    reject_customer,
    save_shop_list,
    set_special_price,
    switch_active_shop,
    update_customer,
)


class CustomerListCreateView(APIView):
    def get_permissions(self):
        if self.request.method == "POST":
            return [role_permission(Role.MANAGER, Role.ACCOUNTANT, Role.REP)()]
        return [IsAuthenticated()]

    @extend_schema(
        summary="List customers for actor scope",
        responses={200: CustomerSerializer(many=True)},
    )
    def get(self, request):
        return ok_response(list_customers_for_actor(actor=request.user))

    @extend_schema(
        summary="Create a customer account",
        request=CustomerInputSerializer,
        responses={201: CustomerSerializer},
    )
    def post(self, request):
        return created_response(create_customer(actor=request.user, data=request.data))


class CustomerDetailUpdateView(APIView):
    def get_permissions(self):
        if self.request.method == "PATCH":
            return [role_permission(Role.MANAGER, Role.ACCOUNTANT)()]
        return [IsAuthenticated()]

    @extend_schema(
        summary="Retrieve customer profile",
        responses={200: CustomerSerializer},
    )
    def get(self, request, customer_id: int):
        return ok_response(get_customer_for_actor(actor=request.user, customer_id=customer_id))

    @extend_schema(
        summary="Update customer profile",
        request=CustomerInputSerializer,
        responses={200: CustomerSerializer},
    )
    def patch(self, request, customer_id: int):
        return ok_response(update_customer(actor=request.user, customer_id=customer_id, data=request.data))


class CustomerRejectView(APIView):
    permission_classes = [role_permission(Role.MANAGER, Role.ACCOUNTANT)]

    @extend_schema(
        summary="Reject pending customer application",
    )
    def post(self, request, customer_id: int):
        reason = request.data.get("reason", "") if isinstance(request.data, dict) else ""
        return ok_response(reject_customer(actor=request.user, customer_id=customer_id, reason=reason))


class CustomerSpecialPricesView(APIView):
    def get_permissions(self):
        if self.request.method == "POST":
            return [role_permission(Role.MANAGER)()]
        return [role_permission(Role.MANAGER, Role.ACCOUNTANT, Role.REP, Role.SHOP)()]

    @extend_schema(
        summary="List customer special prices",
    )
    def get(self, request, customer_id: int):
        return ok_response(list_special_prices(actor=request.user, customer_id=customer_id))

    @extend_schema(
        summary="Set special prices for customer",
    )
    def post(self, request, customer_id: int):
        return ok_response(set_special_price(actor=request.user, customer_id=customer_id, data=request.data))


class MyShopsListCreateView(APIView):
    permission_classes = [role_permission(Role.SHOP)]

    @extend_schema(
        summary="List my registered shops",
        responses={200: MyShopSerializer(many=True)},
    )
    def get(self, request):
        return ok_response(list_my_shops(actor=request.user))

    @extend_schema(
        summary="Add branch shop under current user",
        responses={201: MyShopSerializer},
    )
    def post(self, request):
        return created_response(create_additional_shop(actor=request.user, data=request.data))


class MyShopSwitchView(APIView):
    permission_classes = [role_permission(Role.SHOP)]

    @extend_schema(
        summary="Switch active shop for current session",
        request=MyShopSwitchInputSerializer,
    )
    def post(self, request):
        cid = request.data.get("customer_id") if isinstance(request.data, dict) else None
        sid = getattr(request.user, "_session_id", None)
        return ok_response(
            switch_active_shop(actor=request.user, customer_id=int(cid or 0), session_id=sid, request=request)
        )


class ShopListsView(APIView):
    permission_classes = [role_permission(Role.SHOP)]

    @extend_schema(
        summary="List saved shop lists for active shop",
        responses={200: ShopListSerializer(many=True)},
    )
    def get(self, request):
        return ok_response(list_shop_lists(actor=request.user))

    @extend_schema(
        summary="Create saved shop list",
        request=ShopListInputSerializer,
        responses={201: ShopListSerializer},
    )
    def post(self, request):
        return created_response(save_shop_list(actor=request.user, list_id=None, data=request.data))


class ShopListDetailView(APIView):
    permission_classes = [role_permission(Role.SHOP)]

    @extend_schema(
        summary="Update saved shop list",
        request=ShopListInputSerializer,
        responses={200: ShopListSerializer},
    )
    def patch(self, request, list_id: int):
        return ok_response(save_shop_list(actor=request.user, list_id=list_id, data=request.data))

    @extend_schema(
        summary="Update saved shop list (PUT)",
        request=ShopListInputSerializer,
        responses={200: ShopListSerializer},
    )
    def put(self, request, list_id: int):
        return ok_response(save_shop_list(actor=request.user, list_id=list_id, data=request.data))

    @extend_schema(
        summary="Delete saved shop list",
        responses={200: None},
    )
    def delete(self, request, list_id: int):
        delete_shop_list(actor=request.user, list_id=list_id)
        return ok_response(None)
