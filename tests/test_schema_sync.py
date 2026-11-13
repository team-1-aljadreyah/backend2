"""Schema parity verification ensuring one unified SQL schema stays 1:1 with Django models."""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from django.apps import apps
from django.db import connection

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = REPO_ROOT / "backend" / "database" / "schema.sql"
_EXCLUDED_PARTS = {".venv", "node_modules", ".next", ".git", "__pycache__", "staticfiles", "media"}


def _parse_schema_tables(sql_text: str) -> dict[str, set[str]]:
    tables: dict[str, set[str]] = {}
    pattern = re.compile(r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+([a-zA-Z0-9_]+)\s*\(", re.IGNORECASE)
    for match in pattern.finditer(sql_text):
        table_name = match.group(1)
        pos = match.end()
        depth = 1
        body_chars: list[str] = []
        in_single_quote = False
        while pos < len(sql_text) and depth > 0:
            ch = sql_text[pos]
            if ch == "'" and (pos == 0 or sql_text[pos - 1] != "\\"):
                in_single_quote = not in_single_quote
            elif not in_single_quote:
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                    if depth == 0:
                        break
            body_chars.append(ch)
            pos += 1

        body = "".join(body_chars)
        cols: set[str] = set()
        # Split top-level comma-separated clauses inside CREATE TABLE (...)
        clauses: list[str] = []
        cur: list[str] = []
        c_depth = 0
        c_quote = False
        for ch in body:
            if ch == "'":
                c_quote = not c_quote
            elif not c_quote:
                if ch == "(":
                    c_depth += 1
                elif ch == ")":
                    c_depth -= 1
                elif ch == "," and c_depth == 0:
                    clauses.append("".join(cur))
                    cur = []
                    continue
            cur.append(ch)
        if cur:
            clauses.append("".join(cur))

        for raw_clause in clauses:
            line = raw_clause.strip()
            if not line:
                continue
            upper = line.upper()
            if upper.startswith(("CONSTRAINT ", "PRIMARY KEY", "UNIQUE ", "CHECK ", "FOREIGN KEY", "EXCLUDE ")):
                continue
            col_name = line.split()[0].strip('"')
            cols.add(col_name)
        tables[table_name] = cols
    return tables


def test_single_sql_file_in_repository():
    sql_files = []
    for path in REPO_ROOT.rglob("*.sql"):
        if any(part in _EXCLUDED_PARTS for part in path.parts):
            continue
        sql_files.append(path.relative_to(REPO_ROOT).as_posix())
    assert sql_files == ["backend/database/schema.sql"]


@pytest.mark.django_db
def test_schema_sql_matches_django_models_and_sqlite_tables():
    sql_text = SCHEMA_PATH.read_text(encoding="utf-8")
    schema_tables = _parse_schema_tables(sql_text)

    first_party_models = [m for m in apps.get_models() if m._meta.app_config.name.startswith("apps.")]
    django_table_cols: dict[str, set[str]] = {}
    for model in first_party_models:
        tbl = model._meta.db_table
        cols = {f.column for f in model._meta.concrete_fields}
        django_table_cols[tbl] = cols

    assert set(schema_tables.keys()) == set(django_table_cols.keys())

    with connection.cursor() as cursor:
        existing_db_tables = set(connection.introspection.table_names(cursor))
        for tbl, model_cols in django_table_cols.items():
            assert tbl in existing_db_tables
            sql_cols = schema_tables[tbl]
            assert model_cols == sql_cols, f"Column mismatch on {tbl}: model={model_cols ^ sql_cols}"
            db_cols = {col.name for col in connection.introspection.get_table_description(cursor, tbl)}
            assert model_cols == db_cols, f"Live DB column mismatch on {tbl}: {model_cols ^ db_cols}"

    # Verify PostgreSQL views, immutability triggers, and defense-in-depth RLS declarations exist
    for view_name in ("v_item_stock_summary", "v_invoice_summary", "v_customer_balances", "v_near_expiry_batches"):
        assert f"VIEW {view_name}" in sql_text
    for trig_name in (
        "trg_inventory_movement_immutable",
        "trg_invoices_credit_note_immutable",
        "trg_payments_payment_no_delete",
        "trg_audit_log_immutable",
    ):
        assert f"CREATE TRIGGER {trig_name}" in sql_text
    assert "ENABLE ROW LEVEL SECURITY" in sql_text
    assert "CREATE POLICY " in sql_text
