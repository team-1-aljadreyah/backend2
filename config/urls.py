"""Root URL routing for Project-2 ERP Backend (/api/v1 and operational endpoints)."""
from __future__ import annotations

import mimetypes
from pathlib import Path

from django.conf import settings
from django.contrib import admin
from django.http import FileResponse, HttpResponseForbidden, HttpResponseNotFound
from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

from apps.common.health import LiveHealthView, MetricsView, ReadyHealthView
from apps.common.views import SignedPrivateMediaView


def _serve_public_product_image(request, sub_path: str):
    """Serve ONLY public product-images bucket in local dev (never delivery-proofs!)."""
    public_bucket = getattr(settings, "SUPABASE_BUCKET_PRODUCT_IMAGES", "product-images")
    if not sub_path.startswith(f"{public_bucket}/"):
        return HttpResponseForbidden("Direct access denied")
    base = (Path(settings.MEDIA_ROOT) / public_bucket).resolve()
    rel = sub_path[len(public_bucket) + 1 :]
    target = (base / rel).resolve()
    if target != base and base not in target.parents:
        return HttpResponseForbidden("Invalid path")
    if not target.is_file():
        return HttpResponseNotFound("Not found")
    content_type, _ = mimetypes.guess_type(str(target))
    return FileResponse(target.open("rb"), content_type=content_type or "image/jpeg")


urlpatterns = [
    path("admin/", admin.site.urls),
    path("health", LiveHealthView.as_view(), name="health"),
    path("health/", LiveHealthView.as_view()),
    path("health/live", LiveHealthView.as_view(), name="health-live"),
    path("health/ready", ReadyHealthView.as_view(), name="health-ready"),
    path("metrics", MetricsView.as_view(), name="metrics"),
    path("api/v1/media/signed", SignedPrivateMediaView.as_view(), name="signed-media"),
    path("api/v1/", include("apps.accounts.urls")),
    path("api/v1/", include("apps.warehouses.urls")),
    path("api/v1/", include("apps.catalog.urls")),
    path("api/v1/", include("apps.inventory.urls")),
    path("api/v1/", include("apps.receipts.urls")),
    path("api/v1/", include("apps.supply_orders.urls")),
    path("api/v1/", include("apps.transfers.urls")),
    path("api/v1/", include("apps.stocktakes.urls")),
    path("api/v1/", include("apps.customers.urls")),
    path("api/v1/", include("apps.orders.urls")),
    path("api/v1/", include("apps.deliveries.urls")),
    path("api/v1/", include("apps.invoices.urls")),
    path("api/v1/", include("apps.payments.urls")),
    path("api/v1/", include("apps.commissions.urls")),
    path("api/v1/", include("apps.exchange_rates.urls")),
    path("api/v1/", include("apps.expenses.urls")),
    path("api/v1/", include("apps.reports.urls")),
    path("api/v1/", include("apps.audit.urls")),
    path("api/v1/", include("apps.notifications.urls")),
    path("api/v1/", include("apps.chat.urls")),
    path("api/v1/", include("apps.realtime.urls")),
]

if getattr(settings, "API_ENABLE_DOCS", False):
    urlpatterns += [
        path("api/v1/schema/", SpectacularAPIView.as_view(), name="schema"),
        path("api/v1/docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="swagger-ui"),
    ]

if getattr(settings, "STORAGE_BACKEND", "local") == "local":
    urlpatterns += [
        path("media/<path:sub_path>", _serve_public_product_image, name="local-public-media"),
    ]


handler400 = "apps.common.views.custom_bad_request_handler"
handler403 = "apps.common.views.custom_permission_denied_handler"
handler404 = "apps.common.views.custom_page_not_found_handler"
handler500 = "apps.common.views.custom_server_error_handler"
