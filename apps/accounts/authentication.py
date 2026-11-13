"""Session-aware JWT authentication enforcing active status, token_version, and logout JTI revocation."""
from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from django.core.cache import cache
from django.utils import timezone
from drf_spectacular.extensions import OpenApiAuthenticationExtension
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import AuthenticationFailed, InvalidToken
from rest_framework_simplejwt.tokens import RefreshToken

from apps.accounts.models import RevokedAccessToken, Role, User, UserSession


def _client_ip(request) -> str | None:
    from apps.common.utils import client_ip

    return client_ip(request)


def issue_tokens_for_user(
    user: User,
    *,
    active_customer_id: int | None = None,
    session_id: str | None = None,
    request=None,
) -> tuple[str, str]:
    sid = session_id or uuid4().hex
    if active_customer_id is None and user.role == Role.SHOP:
        from apps.customers.models import ShopMembership

        active_customer_id = (
            ShopMembership.objects.filter(user=user, is_active_shop=True)
            .values_list("customer_id", flat=True)
            .first()
            or ShopMembership.objects.filter(user=user).order_by("-is_primary", "id").values_list("customer_id", flat=True).first()
        )

    refresh = RefreshToken.for_user(user)
    refresh["tv"] = user.token_version
    refresh["sid"] = sid
    access = refresh.access_token
    access["tv"] = user.token_version
    access["sid"] = sid

    ua = (request.META.get("HTTP_USER_AGENT", "")[:255] if request is not None else "")
    ip = _client_ip(request)
    if session_id:
        UserSession.objects.filter(session_id=sid, user=user).update(
            refresh_jti=str(refresh.get("jti", "")),
            last_seen_at=timezone.now(),
        )
    else:
        UserSession.objects.create(
            session_id=sid,
            user=user,
            active_customer_id=active_customer_id,
            refresh_jti=str(refresh.get("jti", "")),
            ip_address=ip,
            user_agent=ua,
        )
    return str(access), str(refresh)


def revoke_access_jti(jti: str, user: User, exp_timestamp: int | float | None) -> None:
    if not jti:
        return
    now = datetime.now(tz=UTC)
    if exp_timestamp is not None:
        exp_dt = datetime.fromtimestamp(float(exp_timestamp), tz=UTC)
    else:
        exp_dt = now
    ttl = max(int((exp_dt - now).total_seconds()), 60)
    cache.set(f"revoked_jti:{jti}", "1", timeout=ttl)
    RevokedAccessToken.objects.get_or_create(
        jti=jti,
        defaults={"user": user, "expires_at": exp_dt},
    )


class SessionAwareJWTAuthentication(JWTAuthentication):
    def authenticate(self, request):
        res = super().authenticate(request)
        if res is None:
            return None
        user, validated_token = res
        from apps.accounts.profiles import enforce_request_profile_access

        enforce_request_profile_access(user, request)
        return user, validated_token

    def get_user(self, validated_token):
        jti = validated_token.get("jti")
        if jti:
            if cache.get(f"revoked_jti:{jti}") or RevokedAccessToken.objects.filter(jti=jti).exists():
                raise AuthenticationFailed("Token has been revoked.", code="token_revoked")

        try:
            user_id = validated_token["user_id"]
        except KeyError as exc:
            raise InvalidToken("Token contained no recognizable user identification") from exc

        try:
            user = User.objects.select_related("warehouse").get(id=user_id)
        except User.DoesNotExist as exc:
            raise AuthenticationFailed("User not found", code="user_not_found") from exc

        if not user.active or user.deleted:
            raise AuthenticationFailed("User is inactive", code="user_inactive")

        from apps.accounts.profiles import in_hours, profile_of

        prof = profile_of(user)
        if prof and not in_hours(prof.get("hours")):
            raise AuthenticationFailed("Outside allowed login hours", code="outside_hours")

        token_version = validated_token.get("tv")
        if token_version is not None and int(token_version) != user.token_version:
            raise AuthenticationFailed("Session expired or invalidated", code="token_version_mismatch")

        sid = validated_token.get("sid")
        if sid:
            sess = (
                UserSession.objects.filter(session_id=str(sid), user_id=user.id)
                .only("id", "revoked", "active_customer_id")
                .first()
            )
            if sess is not None:
                if sess.revoked:
                    raise AuthenticationFailed("Session has been revoked.", code="session_revoked")
                user._session_id = str(sid)
                if user.role == Role.SHOP and sess.active_customer_id is not None:
                    user._session_active_customer_id = sess.active_customer_id

        from apps.accounts.permissions import get_active_customer_id
        from apps.common.rls import apply_pg_session_context

        apply_pg_session_context(
            user_id=user.id,
            role=user.role,
            warehouse_id=user.warehouse_id,
            active_customer_id=get_active_customer_id(user),
        )
        return user


class SessionAwareJWTScheme(OpenApiAuthenticationExtension):
    """Registers Bearer JWT security scheme with drf-spectacular / Swagger UI."""

    target_class = "apps.accounts.authentication.SessionAwareJWTAuthentication"
    name = "jwtAuth"

    def get_security_definition(self, auto_schema):
        return {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "JWT",
        }
