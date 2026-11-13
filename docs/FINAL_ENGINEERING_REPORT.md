# Project-2 ERP Backend — Final Phase 11 Engineering Report

## 1. Executive Summary
The `Al-Nahrain Wholesale ERP` backend (`backend/`) is a Django 5.2 LTS (`5.2.19`) + DRF (`3.18.3`) + Django Channels (`4.3.2`) + Daphne ASGI (`4.2.3`) + Celery (`5.7.0`) modular monolith paired with the Next.js 16 (`16.4.5`) + React 19 (`19.3.0`) frontend (`frontend/`). Under Phase 11 Final Production Readiness Audit and Remediation (executed under Option 2 unversioned workspace isolation), all locally runnable backend and frontend build, lint, schema, API parity, security, WebSocket, and coverage gates were verified passing, with external infrastructure gates explicitly documented as `BLOCKED` pending staging CI execution.

---

## 2. Phase 11 Remediations & Verified Capabilities

1. **Defect Fixed — `/metrics` Scrape Token Authentication ([`backend/apps/common/health.py`](file:///c:/Users/Administrator/Desktop/Alnahrian/backend/apps/common/health.py))**
   - **Root Cause**: `MetricsView` inherited DRF's default `JWTAuthentication`, causing requests carrying static `Authorization: Bearer $METRICS_TOKEN` to fail with `401 Unauthorized` before `check_permissions()` / `_is_authorized()` ran.
   - **Fix**: Added constant-time `_matches_metrics_token(request)` (`hmac.compare_digest`) and `perform_authentication(request)` short-circuiting ahead of JWT auth while preserving `Role.MANAGER` / `Role.ACCOUNTANT` JWT access (`100%` module coverage).

2. **Single Authoritative PostgreSQL Schema ([`backend/database/schema.sql`](file:///c:/Users/Administrator/Desktop/Alnahrian/backend/database/schema.sql))**
   - Verified `backend/database/schema.sql` is the **sole `.sql` file** across the workspace (`tests/test_schema_sync.py`), maintaining column-for-column parity with Django 5.2 models and SQLite introspection.
   - Schema inventory observed in `schema.sql`: **58 tables**, **4 analytical views** (`v_item_stock_summary`, `v_invoice_summary`, `v_customer_balances`, `v_near_expiry_batches`), **6 PL/pgSQL functions** (`erp_set_updated_at()`, `erp_prevent_immutable_mutation()`, `erp_session_user_id()`, `erp_session_role()`, `erp_session_warehouse_id()`, `erp_session_customer_id()`), **17 triggers** (`13` `updated_at` triggers + `4` append-only/no-delete immutability triggers on `inventory_movement`, `invoices_credit_note`, `payments_payment`, `audit_log`), and **43 RLS-enabled tables & policies** bound to session GUCs via [`backend/apps/common/rls.py`](file:///c:/Users/Administrator/Desktop/Alnahrian/backend/apps/common/rls.py).

3. **Durable Transactional Outbox & Realtime Hardening ([`backend/apps/realtime/broadcaster.py`](file:///c:/Users/Administrator/Desktop/Alnahrian/backend/apps/realtime/broadcaster.py), [`backend/apps/realtime/consumers.py`](file:///c:/Users/Administrator/Desktop/Alnahrian/backend/apps/realtime/consumers.py))**
   - `enqueue_outbox_event()` commits `OutboxEvent` inside the DB transaction and schedules delivery strictly on `transaction.on_commit(...)`.
   - `deliver_outbox_event()` enforces atomic row leasing (`OUTBOX_LEASE_SECONDS = 60`), exponential backoff `min(300, 15 * 2^(attempts - 1))`, and dead-letter transition at `OUTBOX_MAX_ATTEMPTS = 5`, paired with 1-minute Celery Beat outbox replay and 15-minute `RealtimeTicket` expiration pruning.
   - Verified ticket replay/expiry/session-revocation (`4001`), strict `Origin` ACL (`4003`), per-user connection limits (`4008`), binary/oversized frame closing (`4009`), rate limiting (`RATE_LIMITED`/`4029`), topic group ACLs (`user_*`, `role_*`, `wh_*`, `shop_*`, `chat_staff`, `loc_staff`), and rep GPS bounds/broadcasting.

4. **Expanded Concurrency Probe Suite & Enterprise Parity Suite (`88%` Backend Coverage)**
   - Expanded [`backend/tests/test_postgres_concurrency.py`](file:///c:/Users/Administrator/Desktop/Alnahrian/backend/tests/test_postgres_concurrency.py) to **7** 20-thread `ThreadPoolExecutor` PostgreSQL contention tests (`@pytest.mark.postgres` skipping cleanly when `TEST_POSTGRES_URL` is unset).
   - Added [`backend/tests/test_phase11_remediation.py`](file:///c:/Users/Administrator/Desktop/Alnahrian/backend/tests/test_phase11_remediation.py) and [`backend/tests/test_enterprise_parity_suite.py`](file:///c:/Users/Administrator/Desktop/Alnahrian/backend/tests/test_enterprise_parity_suite.py) (`6/6` tests covering site branding, custom fields, automation rules, permission profiles/hours/redactions, security health, warehouse layout/cover/delete, quick item & category rename, vehicles/shipment splitting/parent sync, expenses/rep targets/shop self-delete, and WS `?token=` handshake).
   - Latest full pytest run: **`68 passed, 7 skipped` in `30.49s`**, **`88%` statement coverage** (`11,004` statements, `1,295` missed across `apps` + `integrations`).

5. **Full Frontend Static Export Verification (`frontend/`)**
   - Executed `npm ci`, `npx next typegen`, `npx tsc --noEmit`, `npm run lint`, `npm run build` (`56` routes), and `npm run build:pages` (`STATIC_EXPORT=true`, `48` static export routes generating `43` top-level entries under `frontend/out/`, ignored by `frontend/.gitignore`).

---

## 3. Classification & Final Acceptance Gates (Gates 1–14)

**Status Legend**: `IMPLEMENTED` (code/config in repo) · `LOCALLY_TESTED` (executed & verified on this host) · `BLOCKED` (unexecuted on Windows host due to missing DB/Redis/Docker/cloud credentials)

| Gate | Requirement | Classification | Observed Evidence |
| --- | --- | --- | --- |
| 1 | Git & Workspace Isolation (Option 2) | `LOCALLY_TESTED` | Local `.git` not initialized; parent repo untouched; read-only reference clone & pre-Phase-11 backup created |
| 2 | Single SQL Schema & ORM Parity | `LOCALLY_TESTED` | `test_schema_sync.py` PASS (`schema.sql` sole `.sql` file; 58 tables, 43 RLS policies, 17 triggers, 6 functions, 4 views) |
| 3 | Frontend <-> Backend API & Error Parity | `LOCALLY_TESTED` | `test_api_parity.py` (`5/5` PASS: all `http.ts` ops & `146` route handlers with `APPEND_SLASH=False`) + `test_enterprise_parity_suite.py` (`6/6` PASS) |
| 4 | Auth, Multi-Shop Sessions & Redactions | `LOCALLY_TESTED` | Argon2id, `sid` `UserSession`, role redactions (`avg_cost`, `cost`, `profit`, `debt_limit`, `balance`, owning-`shop` `pin`) PASS |
| 5 | Whole-IQD Financial & Ledger Integrity | `LOCALLY_TESTED` | Integer/`Decimal` IQD math, `round_250()`, immutable ledger tests, closed commission months, offline 3-day pricing PASS |
| 6 | Inventory, Supply Orders, Transfers & Stocktakes | `LOCALLY_TESTED` | FEFO deduction, non-negative stock, supply order reconciliation/cancel, transfer review, `countLocked` stocktakes PASS |
| 7 | Delivery QR, 5-Attempt PIN Lock, Media & GPS | `LOCALLY_TESTED` | Persistent 5-attempt PIN lockout, private signed photo URLs, partial delivery splits, 10m live GPS scoping PASS |
| 8 | Realtime WebSocket Security & Outbox | `LOCALLY_TESTED` / `BLOCKED` | `InMemoryChannelLayer` WS consumer/outbox/backoff/ticket tests PASS; live multi-worker `channels_redis` fanout `BLOCKED` |
| 9 | Health, `/metrics` Token Auth & RLS GUCs | `LOCALLY_TESTED` | `/health/live`, `/health/ready`, `/metrics` (`Bearer $METRICS_TOKEN` + admin JWT), `rls.py` GUC lifecycle PASS |
| 10 | Ruff Lint & Django System / Migration Checks | `LOCALLY_TESTED` | `ruff check .`, `manage.py check`, `makemigrations --check --dry-run` all exited `0` |
| 11 | Automated Backend Suite & Coverage | `LOCALLY_TESTED` | `pytest -v --cov=apps --cov=integrations`: **`68 passed, 7 skipped`** in `30.49s`, **`88%`** (`11,004` stmts, `1,295` missed) |
| 12 | Frontend TypeScript, ESLint & Static Export | `LOCALLY_TESTED` | `npm ci`, `next typegen`, `tsc --noEmit`, `eslint`, `next build`, `build:pages` all exited `0` (`frontend/out/index.html` built) |
| 13 | Live PostgreSQL 20-Thread Contention & Docker | `BLOCKED` | `test_postgres_concurrency.py` (7 probes) cleanly `@pytest.mark.postgres` skipped (`TEST_POSTGRES_URL` unset; `psql`/`docker`/`redis-server` absent) |
| 14 | Live External Cloud Integrations | `BLOCKED` | Supabase Storage (`94%`) & Qi Card (`98%`) verified against mocked `httpx`; live sandbox/production credentials absent on host |

---

## 4. Mandatory Staging CI Actions Before Production Cutover
1. Provision PostgreSQL 15+ (with `TEST_POSTGRES_URL`), apply [`backend/database/schema.sql`](file:///c:/Users/Administrator/Desktop/Alnahrian/backend/database/schema.sql), and run `pytest -m postgres` to execute all 7 20-thread concurrency tests and verify native PostgreSQL triggers & 43 RLS policies.
2. Boot `docker compose up --build` (`daphne` + `celery worker` + `celery beat` + `redis` + `postgres`) and verify multi-worker `channels_redis` WebSocket fanout and Celery Beat outbox/ticket schedules.
3. Supply production `DJANGO_SECRET_KEY`, `FIELD_ENCRYPTION_KEY`, `METRICS_TOKEN`, `SUPABASE_*`, `QI_CARD_*`, and Cloudflare Pages `NEXT_PUBLIC_API_URL` / `NEXT_PUBLIC_WS_URL` secrets, then smoke-test live Supabase bucket ACLs and Qi Card webhook delivery.

---

## 5. Documentation Index
- [`backend/docs/PROJECT_CONTEXT.md`](file:///c:/Users/Administrator/Desktop/Alnahrian/backend/docs/PROJECT_CONTEXT.md)
- [`backend/docs/API_CONTRACT.md`](file:///c:/Users/Administrator/Desktop/Alnahrian/backend/docs/API_CONTRACT.md)
- [`backend/docs/API_PARITY_AUDIT.md`](file:///c:/Users/Administrator/Desktop/Alnahrian/backend/docs/API_PARITY_AUDIT.md)
- [`backend/docs/DATABASE_SCHEMA.md`](file:///c:/Users/Administrator/Desktop/Alnahrian/backend/docs/DATABASE_SCHEMA.md)
- [`backend/docs/ARCHITECTURE_DECISIONS.md`](file:///c:/Users/Administrator/Desktop/Alnahrian/backend/docs/ARCHITECTURE_DECISIONS.md)
- [`backend/docs/REALTIME_ARCHITECTURE.md`](file:///c:/Users/Administrator/Desktop/Alnahrian/backend/docs/REALTIME_ARCHITECTURE.md)
- [`backend/docs/PRODUCTION_READINESS.md`](file:///c:/Users/Administrator/Desktop/Alnahrian/backend/docs/PRODUCTION_READINESS.md)
- [`backend/docs/IMPLEMENTATION_STATUS.md`](file:///c:/Users/Administrator/Desktop/Alnahrian/backend/docs/IMPLEMENTATION_STATUS.md)
