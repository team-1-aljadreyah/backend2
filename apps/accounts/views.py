"""HTTP views for authentication, lookups, users, settings, and price tiers."""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import status as http_status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken

from apps.accounts.authentication import issue_tokens_for_user, revoke_access_jti
from apps.accounts.health import build_health_report
from apps.accounts.models import Role, UserSession
from apps.accounts.permissions import role_permission
from apps.accounts.profiles import get_site_payload, save_custom_values, save_site_config
from apps.accounts.rules_engine import list_recent_rule_runs
from apps.accounts.serializers import (
    AuthTokensSerializer,
    ChangePasswordInputSerializer,
    CreateUserInputSerializer,
    LoginInputSerializer,
    PriceTierSerializer,
    RefreshInputSerializer,
    SettingsSerializer,
    SiteCustomSerializer,
    UpdateUserInputSerializer,
    UserSerializer,
    serialize_user,
)
from apps.accounts.services import (
    build_lookups,
    change_my_password,
    create_price_tier,
    create_user_by_manager,
    delete_my_account,
    delete_user_by_manager,
    list_all_users,
    login_with_phone,
    register_shop_account,
    remove_my_shop,
    remove_user_photo,
    set_my_photo,
    set_user_profile,
    update_price_tier,
    update_settings_by_manager,
    update_user_by_manager,
)
from apps.accounts.throttles import LoginRateThrottle, RegisterRateThrottle
from apps.common.exceptions import ApiException, ErrorCode
from apps.common.responses import created_response, ok_response

User = get_user_model()


class LoginView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [LoginRateThrottle]

    @extend_schema(
        summary="User phone & password login",
        request=LoginInputSerializer,
        responses={200: AuthTokensSerializer},
    )
    def post(self, request):
        phone = request.data.get("phone")
        password = str(request.data.get("password") or "")
        return ok_response(login_with_phone(phone, password, request=request))


class RegisterView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [RegisterRateThrottle]

    @extend_schema(
        summary="Shop owner account self-registration",
        responses={201: AuthTokensSerializer},
    )
    def post(self, request):
        return created_response(register_shop_account(request.data, request=request))


class LogoutView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="Revoke JWT tokens and logout session",
        responses={200: dict},
    )
    def post(self, request):
        token = request.auth
        sid = None
        if token is not None:
            jti = token.get("jti")
            exp = token.get("exp")
            sid = token.get("sid")
            revoke_access_jti(jti, request.user, exp)

        if sid:
            UserSession.objects.filter(session_id=str(sid), user=request.user).update(
                revoked=True, revoked_at=timezone.now()
            )

        refresh_raw = request.data.get("refresh") if isinstance(request.data, dict) else None
        if refresh_raw:
            try:
                ref_tok = RefreshToken(str(refresh_raw))
                ref_sid = ref_tok.get("sid")
                if ref_sid:
                    UserSession.objects.filter(session_id=str(ref_sid), user=request.user).update(
                        revoked=True, revoked_at=timezone.now()
                    )
                ref_tok.blacklist()
            except TokenError:
                pass

        return ok_response({"ok": True})


class RefreshTokenView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    @extend_schema(
        summary="Refresh access token using refresh token",
        request=RefreshInputSerializer,
        responses={200: AuthTokensSerializer},
    )
    def post(self, request):
        raw_refresh = request.data.get("refresh") if isinstance(request.data, dict) else None
        if not raw_refresh:
            raise ApiException(
                code=ErrorCode.UNAUTHENTICATED,
                message=("رمز التحديث مطلوب", "Refresh token is required"),
                status_code=http_status.HTTP_401_UNAUTHORIZED,
            )
        try:
            old_refresh = RefreshToken(str(raw_refresh))
            old_refresh.check_blacklist()
            user_id = old_refresh["user_id"]
            token_version = old_refresh.get("tv")
            sid = old_refresh.get("sid")
            user = User.objects.get(id=user_id)
            if not user.active or (token_version is not None and int(token_version) != user.token_version):
                raise TokenError("User inactive or token version changed")
            if sid:
                sess = UserSession.objects.filter(session_id=str(sid), user=user).first()
                if sess and sess.revoked:
                    raise TokenError("Session revoked")
            old_refresh.blacklist()
            new_access, new_refresh = issue_tokens_for_user(
                user, session_id=str(sid) if sid else None, request=request
            )
            return ok_response({"token": new_access, "refresh": new_refresh})
        except (TokenError, User.DoesNotExist) as exc:
            raise ApiException(
                code=ErrorCode.UNAUTHENTICATED,
                message=("جلسة الدخول منتهية الصلاحية", "Refresh token is invalid or expired"),
                status_code=http_status.HTTP_401_UNAUTHORIZED,
            ) from exc


class MeView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="Get active user profile and permissions",
        responses={200: UserSerializer},
    )
    def get(self, request):
        return ok_response(serialize_user(request.user, request=request))


class MePhotoView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="Upload personal profile photo",
        responses={200: UserSerializer},
    )
    def post(self, request):
        photo = request.data.get("photo") if isinstance(request.data, dict) else None
        return ok_response(set_my_photo(actor=request.user, photo=photo, request=request))


class MePasswordView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="Change current user password",
        request=ChangePasswordInputSerializer,
        responses={200: dict},
    )
    def post(self, request):
        current = str((request.data or {}).get("current") or "")
        next_pw = str((request.data or {}).get("next") or "")
        return ok_response(change_my_password(actor=request.user, current_password=current, new_password=next_pw))


class MeShopRemoveView(APIView):
    permission_classes = [role_permission(Role.SHOP)]

    @extend_schema(
        summary="Remove shop membership from active user",
        responses={200: dict},
    )
    def post(self, request, customer_id: int):
        password = str((request.data or {}).get("password") or "")
        return ok_response(remove_my_shop(actor=request.user, customer_id=customer_id, password=password, request=request))


class MeDeleteView(APIView):
    permission_classes = [role_permission(Role.SHOP)]

    @extend_schema(
        summary="Delete active shop account",
        responses={200: dict},
    )
    def post(self, request):
        password = str((request.data or {}).get("password") or "")
        return ok_response(delete_my_account(actor=request.user, password=password))


class LookupsView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="Cached initial application lookups (warehouses, vehicles, tiers, reps)",
        responses={200: dict},
    )
    def get(self, request):
        return ok_response(build_lookups(request.user))


class UsersListCreateView(APIView):
    permission_classes = [role_permission(Role.MANAGER)]

    @extend_schema(
        summary="List all company users",
        responses={200: UserSerializer(many=True)},
    )
    def get(self, request):
        return ok_response(list_all_users())

    @extend_schema(
        summary="Create company staff/user",
        request=CreateUserInputSerializer,
        responses={201: UserSerializer},
    )
    def post(self, request):
        return created_response(create_user_by_manager(actor=request.user, data=request.data))


class UserDetailView(APIView):
    permission_classes = [role_permission(Role.MANAGER)]

    @extend_schema(
        summary="Update company user attributes",
        request=UpdateUserInputSerializer,
        responses={200: UserSerializer},
    )
    def patch(self, request, user_id: int):
        return ok_response(update_user_by_manager(actor=request.user, user_id=user_id, data=request.data))

    @extend_schema(
        summary="Delete user (anonymize/soft delete)",
        responses={200: dict},
    )
    def delete(self, request, user_id: int):
        delete_user_by_manager(actor=request.user, user_id=user_id)
        return ok_response({"ok": True})


class UserProfileView(APIView):
    permission_classes = [role_permission(Role.MANAGER)]

    @extend_schema(
        summary="Assign permission profile to user",
        responses={200: UserSerializer},
    )
    def post(self, request, user_id: int):
        profile_id = (request.data or {}).get("profile_id")
        return ok_response(set_user_profile(actor=request.user, user_id=user_id, profile_id=profile_id))


class UserPhotoRemoveView(APIView):
    permission_classes = [role_permission(Role.MANAGER)]

    @extend_schema(
        summary="Remove user profile photo",
        responses={200: UserSerializer},
    )
    def delete(self, request, user_id: int):
        return ok_response(remove_user_photo(actor=request.user, user_id=user_id))


class SettingsUpdateView(APIView):
    permission_classes = [role_permission(Role.MANAGER)]

    @extend_schema(
        summary="Update system operational settings",
        request=SettingsSerializer,
        responses={200: SettingsSerializer},
    )
    def patch(self, request):
        return ok_response(update_settings_by_manager(actor=request.user, data=request.data))


class SitePublicView(APIView):
    permission_classes = [AllowAny]

    @extend_schema(
        summary="Get site configuration, branding, navigation, and banners",
        responses={200: SiteCustomSerializer},
    )
    def get(self, request):
        include_adv = bool(request.user and request.user.is_authenticated)
        return ok_response(get_site_payload(request=request, include_advanced=include_adv))


class SiteUpdateView(APIView):
    permission_classes = [role_permission(Role.MANAGER)]

    @extend_schema(
        summary="Update site configuration, branding, navigation, and banners",
        request=SiteCustomSerializer,
        responses={200: SiteCustomSerializer},
    )
    def patch(self, request):
        return ok_response(save_site_config(actor=request.user, data=request.data, request=request))


class CustomValuesView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="Save entity custom field values",
        responses={200: dict},
    )
    def patch(self, request, object_type: str, object_id: int):
        return ok_response(
            save_custom_values(
                actor=request.user,
                object_type=object_type,
                object_id=object_id,
                values=request.data if isinstance(request.data, dict) else {},
            )
        )


class RuleRunsView(APIView):
    permission_classes = [role_permission(Role.MANAGER)]

    @extend_schema(
        summary="List recent automation rule execution logs",
        responses={200: list},
    )
    def get(self, request):
        return ok_response(list_recent_rule_runs(limit=100))


class SetupHealthView(APIView):
    permission_classes = [role_permission(Role.MANAGER)]

    @extend_schema(
        summary="System setup health verification report",
        responses={200: dict},
    )
    def get(self, request):
        return ok_response(build_health_report())


class PriceTierCreateView(APIView):
    permission_classes = [role_permission(Role.MANAGER)]

    @extend_schema(
        summary="Create customer price tier",
        responses={201: PriceTierSerializer},
    )
    def post(self, request):
        return created_response(create_price_tier(actor=request.user, data=request.data))


class PriceTierUpdateView(APIView):
    permission_classes = [role_permission(Role.MANAGER)]

    @extend_schema(
        summary="Update customer price tier name",
        responses={200: PriceTierSerializer},
    )
    def patch(self, request, tier_id: int):
        return ok_response(update_price_tier(actor=request.user, tier_id=tier_id, data=request.data))
