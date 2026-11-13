"""PostgreSQL transaction/session RLS GUC helpers (defense-in-depth multi-tenant isolation)."""
from __future__ import annotations

from contextlib import contextmanager
from typing import Any

from django.db import connection


def apply_pg_session_context(
    *,
    user_id: int | None = None,
    role: str | None = None,
    warehouse_id: int | None = None,
    active_customer_id: int | None = None,
) -> None:
    """
    Bind PostgreSQL session/local GUCs used by schema.sql RLS helpers:
    `app.user_id`, `app.role`, `app.warehouse_id`, `app.active_customer_id`.
    No-op on SQLite.
    """
    if connection.vendor != "postgresql":
        return
    is_local = bool(connection.in_atomic_block)
    params = (
        str(user_id) if user_id else "",
        is_local,
        str(role) if role else "",
        is_local,
        str(warehouse_id) if warehouse_id else "",
        is_local,
        str(active_customer_id) if active_customer_id else "",
        is_local,
    )
    with connection.cursor() as cur:
        cur.execute(
            "SELECT "
            "set_config('app.user_id', %s, %s), "
            "set_config('app.role', %s, %s), "
            "set_config('app.warehouse_id', %s, %s), "
            "set_config('app.active_customer_id', %s, %s)",
            params,
        )


def clear_pg_session_context() -> None:
    """Clear all `app.*` PostgreSQL GUCs at request boundaries so pooled connections stay clean."""
    if connection.vendor != "postgresql":
        return
    with connection.cursor() as cur:
        cur.execute(
            "SELECT "
            "set_config('app.user_id', '', false), "
            "set_config('app.role', '', false), "
            "set_config('app.warehouse_id', '', false), "
            "set_config('app.active_customer_id', '', false)"
        )


@contextmanager
def pg_rls_context(user: Any, active_customer_id: int | None = None):
    """Scoped context manager binding PostgreSQL RLS GUCs for background tasks/jobs."""
    apply_pg_session_context(
        user_id=getattr(user, "id", None),
        role=getattr(user, "role", None),
        warehouse_id=getattr(user, "warehouse_id", None),
        active_customer_id=active_customer_id,
    )
    try:
        yield
    finally:
        clear_pg_session_context()
