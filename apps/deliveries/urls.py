"""URL routes for deliveries under /api/v1/."""
from django.urls import path

from .views import (
    DeliveryDispatchView,
    DeliveryLoadView,
    DeliveryPhotoView,
    DeliveryProblemView,
    DeliveryReturnToWarehouseView,
    DeliveryUnlockView,
    OrderDeliverView,
    RepLocationListView,
    RepLocationUpdateView,
    VehicleCreateView,
    VehicleUpdateView,
)

urlpatterns = [
    path("vehicles", VehicleCreateView.as_view(), name="vehicles-create"),
    path("vehicles/<int:vehicle_id>", VehicleUpdateView.as_view(), name="vehicles-update"),
    path("deliveries/dispatch", DeliveryDispatchView.as_view(), name="deliveries-dispatch"),
    path("deliveries/problem", DeliveryProblemView.as_view(), name="deliveries-problem"),
    path("deliveries/location", RepLocationUpdateView.as_view(), name="deliveries-location-update"),
    path("deliveries/locations", RepLocationListView.as_view(), name="deliveries-location-list"),
    path("orders/<int:order_id>/deliver", OrderDeliverView.as_view(), name="order-deliver"),
    path("orders/<int:order_id>/delivery/load", DeliveryLoadView.as_view(), name="order-delivery-load"),
    path("orders/<int:order_id>/delivery/unlock", DeliveryUnlockView.as_view(), name="order-delivery-unlock"),
    path("orders/<int:order_id>/delivery/return", DeliveryReturnToWarehouseView.as_view(), name="order-delivery-return"),
    path("orders/<int:order_id>/delivery/photo", DeliveryPhotoView.as_view(), name="order-delivery-photo"),
]
