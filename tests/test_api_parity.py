"""Automated API & WebSocket contract parity check against frontend/lib/api/http.ts and realtime.ts."""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from django.apps import apps
from django.urls import Resolver404, resolve
from rest_framework.test import APIClient

from apps.accounts.models import PriceTier, Role, SystemSetting, User
from apps.common.exceptions import ErrorCode

REPO_ROOT = Path(__file__).resolve().parents[2]
HTTP_TS = REPO_ROOT / "frontend" / "lib" / "api" / "http.ts"
ERRORS_TS = REPO_ROOT / "frontend" / "lib" / "api" / "errors.ts"
REALTIME_TS = REPO_ROOT / "frontend" / "lib" / "realtime.ts"


def _parse_http_ts_endpoints() -> list[tuple[str, str, str]]:
    """
    Parse every operation in `frontend/lib/api/http.ts` (`httpApi = { ... }`)
    into `(fn_name, http_method, path_template)` tuples, handling multiline signatures
    and nested TypeScript generic object types cleanly.
    """
    src = HTTP_TS.read_text(encoding="utf-8")
    body = src.split("export const httpApi = {", 1)[1].split("export type Api =", 1)[0]
    entries: list[tuple[str, str, str]] = []

    call_re = re.compile(r"=>\s*(?:\n\s*)?(get|post|patch|del|save)<")
    # Match each top-level `  fnName: ... => get|post|patch|del|save<...>(path` cleanly
    top_fn_re = re.compile(r"^\s{2}([a-zA-Z0-9_]+):\s*", re.MULTILINE)

    pos = 0
    while True:
        fm = top_fn_re.search(body, pos)
        if not fm:
            break
        fn_name = fm.group(1)
        if fn_name == "resetDemo":
            pos = fm.end()
            continue
        cm = call_re.search(body, fm.end())
        if not cm:
            break
        helper = cm.group(1)
        # Walk past balanced `<...>` TypeScript generic type to find `(` argument list
        i = cm.end()
        depth = 1
        while i < len(body) and depth > 0:
            if body[i] == "<":
                depth += 1
            elif body[i] == ">" and body[i - 1 : i + 1] != "=>":
                depth -= 1
            i += 1
        while i < len(body) and body[i] != "(":
            i += 1
        arg_m = re.match(r"\(\s*([`\"'])(.*?)\1", body[i:])
        assert arg_m, f"Unable to parse path literal for `{fn_name}` near `{body[i:i+60]}`"
        path_tmpl = arg_m.group(2)
        pos = i + arg_m.end()

        if helper == "get":
            entries.append((fn_name, "GET", path_tmpl))
        elif helper == "post":
            entries.append((fn_name, "POST", path_tmpl))
        elif helper == "patch":
            entries.append((fn_name, "PATCH", path_tmpl))
        elif helper == "del":
            entries.append((fn_name, "DELETE", path_tmpl))
        elif helper == "save":
            entries.append((f"{fn_name}:create", "POST", path_tmpl))
            entries.append((f"{fn_name}:update", "PATCH", f"{path_tmpl}/${{id}}"))
    return entries


def _materialize_path(tmpl: str) -> str:
    """Replace TypeScript `${id}` string template variables with concrete sample URL segments."""
    if tmpl == "/payments/online/${id}":
        return "/api/v1/payments/online/sample_req_1"
    concrete = re.sub(r"\$\{[^}]+\}", "1", tmpl)
    return f"/api/v1{concrete}"


def test_error_codes_match_frontend_errors_ts():
    src = ERRORS_TS.read_text(encoding="utf-8")
    ts_codes = set(re.findall(r'\|\s*"([A-Z_]+)"', src))
    py_codes = {
        getattr(ErrorCode, attr)
        for attr in dir(ErrorCode)
        if attr.isupper() and isinstance(getattr(ErrorCode, attr), str)
    }
    assert ts_codes == py_codes


def test_every_frontend_http_operation_resolves_to_matching_django_cbv_method():
    entries = _parse_http_ts_endpoints()
    assert len(entries) >= 130

    # Verify resetDemo is the ONLY frontend-only helper skipped
    assert HTTP_TS.read_text(encoding="utf-8").count("resetDemo:") == 1

    for fn_name, method, tmpl in entries:
        url_path = _materialize_path(tmpl)
        try:
            match = resolve(url_path)
        except Resolver404 as exc:
            pytest.fail(f"Frontend endpoint `{fn_name}` ({method} {url_path}) missing in Django URLs: {exc}")

        view_cls = getattr(match.func, "view_class", None)
        assert view_cls is not None, f"{fn_name} -> {url_path} is not mapped to a DRF APIView class"
        handler_name = method.lower()
        assert hasattr(view_cls, handler_name), (
            f"Frontend `{fn_name}` calls {method} {url_path}, but {view_cls.__name__} lacks `{handler_name}()`"
        )


def test_realtime_websocket_and_ticket_paths_match_frontend():
    rt_src = REALTIME_TS.read_text(encoding="utf-8")
    assert "token=${encodeURIComponent(token)}" in rt_src
    assert 'action: "subscribe"' in rt_src

    match = resolve("/api/v1/realtime/ticket")
    assert hasattr(match.func.view_class, "post")

    from apps.realtime.routing import websocket_urlpatterns

    ws_routes = [str(p.pattern) for p in websocket_urlpatterns]
    assert any("ws" in pat for pat in ws_routes)


@pytest.mark.django_db
def test_protected_endpoints_enforce_unauthenticated_401_and_trailing_slash_off():
    entries = _parse_http_ts_endpoints()
    anon = APIClient()
    public_paths = {"/api/v1/auth/login", "/api/v1/auth/register", "/api/v1/site", "/api/v1/catalog"}

    for fn_name, method, tmpl in entries:
        url_path = _materialize_path(tmpl)
        if url_path in public_paths:
            continue
        # Ensure route resolves strictly without trailing slash (APPEND_SLASH = False)
        assert not url_path.endswith("/")
        res = getattr(anon, method.lower())(url_path, format="json")
        assert res.status_code == 401, f"{fn_name} ({method} {url_path}) allowed anonymous access: {res.status_code}"
        body = res.json()
        assert body["success"] is False and body["code"] == "UNAUTHENTICATED"


@pytest.mark.django_db
def test_envelope_contract_on_core_routes():
    PriceTier.objects.create(name="جملة", name_en="Wholesale", sort_order=1)
    SystemSetting.get_solo()
    User.objects.create_user(phone="07740000001", password="Password123", name="Mgr", role=Role.MANAGER)

    cli = APIClient()
    login = cli.post("/api/v1/auth/login", {"phone": "07740000001", "password": "Password123"}, format="json")
    assert login.status_code == 200
    l_json = login.json()
    assert l_json["success"] is True and "token" in l_json["data"] and "user" in l_json["data"]

    cli.credentials(HTTP_AUTHORIZATION=f"Bearer {l_json['data']['token']}")
    for ep in ("/api/v1/me", "/api/v1/lookups", "/api/v1/users", "/api/v1/items", "/api/v1/customers"):
        r = cli.get(ep)
        assert r.status_code == 200
        assert r.json()["success"] is True and "data" in r.json()
    assert apps.is_installed("apps.realtime")
