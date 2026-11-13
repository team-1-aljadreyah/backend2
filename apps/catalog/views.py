"""Catalog HTTP views with drf-spectacular schemas."""
from __future__ import annotations

from drf_spectacular.utils import extend_schema
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.views import APIView

from apps.accounts.models import Role
from apps.accounts.permissions import role_permission
from apps.catalog.serializers import (
    CatalogItemSerializer,
    CategoryRenameInputSerializer,
    ItemDeleteResponseSerializer,
    ItemInputSerializer,
    ItemQuickUpdateSerializer,
    ItemSerializer,
    ItemVideoInputSerializer,
)
from apps.catalog.services import (
    create_item,
    delete_item,
    get_catalog,
    get_item_for_actor,
    list_items_for_actor,
    quick_update_item,
    rename_category,
    set_item_video,
    update_item,
    update_item_image,
)
from apps.common.responses import created_response, ok_response
from apps.inventory.serializers import BatchSerializer
from apps.inventory.services import list_batches_for_actor


class CatalogListView(APIView):
    """Public catalog endpoint: active items with images and packaging, no prices or costs."""

    permission_classes = [AllowAny]

    @extend_schema(
        summary="Public catalog listing",
        responses={200: CatalogItemSerializer(many=True)},
        auth=[],
    )
    def get(self, request):
        return ok_response(get_catalog(request=request))


class ItemListCreateView(APIView):
    def get_permissions(self):
        if self.request.method == "POST":
            return [role_permission(Role.MANAGER)()]
        return [IsAuthenticated()]

    @extend_schema(
        summary="List all items",
        responses={200: ItemSerializer(many=True)},
    )
    def get(self, request):
        return ok_response(list_items_for_actor(actor=request.user, request=request))

    @extend_schema(
        summary="Create a catalog item",
        request=ItemInputSerializer,
        responses={201: ItemSerializer},
    )
    def post(self, request):
        return created_response(create_item(actor=request.user, data=request.data, request=request))


class ItemDetailView(APIView):
    def get_permissions(self):
        if self.request.method in ("PATCH", "DELETE"):
            return [role_permission(Role.MANAGER)()]
        return [IsAuthenticated()]

    @extend_schema(
        summary="Retrieve item details",
        responses={200: ItemSerializer},
    )
    def get(self, request, item_id: int):
        return ok_response(get_item_for_actor(actor=request.user, item_id=item_id, request=request))

    @extend_schema(
        summary="Update item",
        request=ItemInputSerializer,
        responses={200: ItemSerializer},
    )
    def patch(self, request, item_id: int):
        return ok_response(update_item(actor=request.user, item_id=item_id, data=request.data, request=request))

    @extend_schema(
        summary="Delete or hide item",
        responses={200: ItemDeleteResponseSerializer},
    )
    def delete(self, request, item_id: int):
        return ok_response(delete_item(actor=request.user, item_id=item_id))


class ItemVideoView(APIView):
    permission_classes = [role_permission(Role.MANAGER, Role.WAREHOUSE)]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    @extend_schema(
        summary="Upload or set item video link",
        request=ItemVideoInputSerializer,
        responses={200: ItemSerializer},
    )
    def post(self, request, item_id: int):
        uploaded_file = request.FILES.get("file") or request.FILES.get("video")
        video_val = None
        if isinstance(request.data, dict) and "video" in request.data and not uploaded_file:
            video_val = request.data.get("video")
        return ok_response(
            set_item_video(
                actor=request.user,
                item_id=item_id,
                video_value=video_val,
                uploaded_file=uploaded_file,
                request=request,
            )
        )


class ItemImageUploadView(APIView):
    permission_classes = [role_permission(Role.MANAGER, Role.WAREHOUSE)]

    @extend_schema(
        summary="Upload item image (Base64 data URL)",
        responses={200: ItemSerializer},
    )
    def post(self, request, item_id: int):
        image = request.data.get("image") if isinstance(request.data, dict) else None
        return ok_response(update_item_image(actor=request.user, item_id=item_id, image_data_url=image, request=request))


class ItemBatchesView(APIView):
    permission_classes = [role_permission(Role.MANAGER, Role.ACCOUNTANT, Role.WAREHOUSE, Role.STAFF)]

    @extend_schema(
        summary="List batches for an item",
        responses={200: BatchSerializer(many=True)},
    )
    def get(self, request, item_id: int):
        from rest_framework import status as http_status

        from apps.catalog.models import Item
        from apps.common.exceptions import ApiException, ErrorCode

        if not Item.objects.filter(id=item_id).exists():
            raise ApiException(code=ErrorCode.NOT_FOUND, status_code=http_status.HTTP_404_NOT_FOUND)
        return ok_response(list_batches_for_actor(actor=request.user, item_id=item_id))


class ItemQuickUpdateView(APIView):
    permission_classes = [role_permission(Role.MANAGER)]

    @extend_schema(
        summary="Quick edit item attributes or tier prices",
        request=ItemQuickUpdateSerializer,
        responses={200: ItemSerializer},
    )
    def patch(self, request, item_id: int):
        return ok_response(quick_update_item(actor=request.user, item_id=item_id, data=request.data, request=request))


class CategoryRenameView(APIView):
    permission_classes = [role_permission(Role.MANAGER)]

    @extend_schema(
        summary="Rename category across items, reps, and site settings",
        request=CategoryRenameInputSerializer,
    )
    def post(self, request):
        data = request.data if isinstance(request.data, dict) else {}
        return ok_response(
            rename_category(
                actor=request.user,
                old_name=str(data.get("from") or data.get("old") or ""),
                new_name=str(data.get("to") or data.get("next") or ""),
            )
        )
