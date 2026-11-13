"""DRF views and URLs for immutable Audit Log."""
from __future__ import annotations

from django.urls import path
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.views import APIView

from apps.audit.services import list_audit_for_actor
from apps.common.responses import ok_response


class AuditListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request: Request):
        raw_lim = request.query_params.get("limit") or "300"
        try:
            limit = int(raw_lim)
        except ValueError:
            limit = 300
        return ok_response(
            data=list_audit_for_actor(
                actor=request.user,
                limit=limit,
                action=request.query_params.get("action") or None,
                entity=request.query_params.get("entity") or None,
            )
        )


urlpatterns = [
    path("audit", AuditListView.as_view(), name="audit-list"),
]
