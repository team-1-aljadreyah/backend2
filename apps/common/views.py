"""Signed private media retrieval for local storage dev parity."""
from __future__ import annotations

import mimetypes
from pathlib import Path

from django.conf import settings
from django.core.signing import BadSignature, SignatureExpired, TimestampSigner
from django.http import FileResponse, HttpResponseForbidden, HttpResponseNotFound, JsonResponse
from rest_framework.permissions import AllowAny
from rest_framework.views import APIView


class SignedPrivateMediaView(APIView):
    """Serves locally stored private media (e.g. delivery-proofs) ONLY via short-lived HMAC token."""

    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request):
        token = request.query_params.get("token", "")
        if not token:
            return HttpResponseForbidden("Missing token")
        signer = TimestampSigner(salt="local-private-media")
        ttl = int(getattr(settings, "SIGNED_URL_TTL_SECONDS", 900))
        try:
            rel_path = signer.unsign(token, max_age=ttl)
        except (BadSignature, SignatureExpired):
            return HttpResponseForbidden("Invalid or expired media token")

        base = Path(settings.MEDIA_ROOT).resolve()
        target = (base / rel_path).resolve()
        if target != base and base not in target.parents:
            return HttpResponseForbidden("Invalid path")
        if not target.is_file():
            return HttpResponseNotFound("File not found")

        content_type, _ = mimetypes.guess_type(str(target))
        resp = FileResponse(target.open("rb"), content_type=content_type or "application/octet-stream")
        resp["Cache-Control"] = "private, max-age=60"
        return resp


def custom_bad_request_handler(request, exception=None):
    return JsonResponse({"success": False, "code": "BAD_REQUEST", "message": "Bad request"}, status=400)


def custom_permission_denied_handler(request, exception=None):
    return JsonResponse({"success": False, "code": "FORBIDDEN", "message": "Forbidden"}, status=403)


def custom_page_not_found_handler(request, exception=None):
    return JsonResponse({"success": False, "code": "NOT_FOUND", "message": "Not found"}, status=404)


def custom_server_error_handler(request):
    return JsonResponse({"success": False, "code": "SERVER_ERROR", "message": "Internal server error"}, status=500)
