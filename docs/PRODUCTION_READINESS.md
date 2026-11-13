# Production Readiness & Deployment Guide

> Target Architecture: Independently deployed Django 5.2 LTS ASGI backend (`Daphne` + `Django Channels` + `Celery`) backed by Supabase PostgreSQL 15+, Supabase Storage, and Redis, serving the static Next.js 16 frontend deployed on Cloudflare Pages.

---

## 1. Runtime Topology & Containers

Defined in [`backend/Dockerfile`](file:///c:/Users/Administrator/Desktop/Alnahrian/backend/Dockerfile) and [`backend/docker-compose.yml`](file:///c:/Users/Administrator/Desktop/Alnahrian/backend/docker-compose.yml):

| Service / Process | Command | Responsibility |
| --- | --- | --- |
| `web` (ASGI) | `daphne -b 0.0.0.0 -p 8000 config.asgi:application` | Serves `/api/v1/*` HTTP REST + `/ws` WebSockets + `/health/*` |
| `worker` (Celery) | `celery -A config worker -l info` | Async background jobs & outbox retries |
| `beat` (Celery Beat) | `celery -A config beat -l info` | Periodic schedules (see §4) |
| PostgreSQL 15+ / Supabase | External managed Postgres or `postgres:16-alpine` | Primary transactional store with `backend/database/schema.sql` constraints, views, triggers & RLS |
| Redis | `redis:7-alpine` | Cache (`/0`), Celery broker (`/1`), Celery results (`/2`), Channels layer (`/3`) |

---

## 2. Mandatory Environment Configuration (`backend/.env.example`)

Before starting in `DJANGO_ENV=production` (`config/settings/production.py`), set strong non-empty values for:

| Variable | Production Requirement |
| --- | --- |
| `DJANGO_ENV` | `production` (enables strict SSL/cookie/CSRF hardening and blocks `seed_demo`) |
| `DJANGO_SECRET_KEY` | High-entropy random secret (>= 50 chars) |
| `DJANGO_ALLOWED_HOSTS` | Exact API hostname(s), e.g. `api.alnahrain.example` |
| `CORS_ALLOWED_ORIGINS` | Exact Cloudflare Pages origin(s), e.g. `https://app.alnahrain.example` (used for CORS + WS `Origin` validation) |
| `CSRF_TRUSTED_ORIGINS` | Matching HTTPS frontend & API origins |
| `DB_ENGINE` | `postgres` |
| `DATABASE_URL` | Supabase PostgreSQL connection string (`?sslmode=require`) |
| `DB_CONN_MAX_AGE` / `DB_SSL_REQUIRE` | `60` (or `0` when behind transaction-mode PgBouncer), `true` |
| `FIELD_ENCRYPTION_KEY` | Valid `Fernet` key for `deliveries_delivery.pin_encrypted` |
| `STORAGE_BACKEND` | `supabase` |
| `SUPABASE_URL` & `SUPABASE_SERVICE_ROLE_KEY` | Supabase project URL and **server-only** service role key |
| `SUPABASE_BUCKET_PRODUCT_IMAGES` | Public catalog bucket (`product-images`) |
| `SUPABASE_BUCKET_DELIVERY_PROOFS` | Private bucket (`delivery-proofs`, signed URL TTL `SIGNED_URL_TTL_SECONDS=900`) |
| `REDIS_URL` / `CELERY_*` / `CHANNELS_REDIS_URL` | Isolated Redis DB indices `/0`..`/3`; set `CELERY_TASK_ALWAYS_EAGER=false` |
| `QI_CARD_MODE` & credentials | Set `QI_CARD_BASE_URL`, `QI_CARD_MERCHANT_ID`, `QI_CARD_TERMINAL_ID`, `QI_CARD_API_KEY`, `QI_CARD_WEBHOOK_SECRET` when enabling live payments (stays fail-closed when `disabled`) |
| `METRICS_TOKEN` | Bearer secret protecting `GET /metrics` for non-manager scrapers |
| `API_ENABLE_DOCS` | Recommended `false` in public production unless restricted behind VPN/auth |

---

## 3. Database & Supabase Security Notes

1. **PgBouncer Pooling**: If connecting to Supabase's transaction pooler (port `6543`), set `DB_CONN_MAX_AGE=0` or disable server-side prepared statements in psycopg options as appropriate, whereas direct session pooler / port `5432` works directly with `CONN_MAX_AGE=60` and `CONN_HEALTH_CHECKS=True`.
2. **Unified Schema & Migrations**: Apply Django migrations (`python manage.py migrate --noinput`) or provision PostgreSQL from the single unified DDL file [`backend/database/schema.sql`](file:///c:/Users/Administrator/Desktop/Alnahrian/backend/database/schema.sql) (52 tables, 4 analytical views, 13 triggers including `trg_inventory_movement_immutable`, `trg_invoices_credit_note_immutable`, `trg_payments_payment_no_delete`, `trg_audit_log_immutable`, and 37 RLS-enabled tables/policies).
3. **RLS Defense-in-Depth**: Supabase PostgREST / non-owner roles without `BYPASSRLS` are strictly scoped by `erp_session_user_id()`, `erp_session_role()`, `erp_session_warehouse_id()`, and `erp_session_customer_id()` reading `app.user_id`, `app.role`, `app.warehouse_id`, and `app.active_customer_id`. On Django request/task boundaries, [`backend/apps/common/rls.py`](file:///c:/Users/Administrator/Desktop/Alnahrian/backend/apps/common/rls.py) binds these GUCs via `JwtAuthentication`, `switch_active_shop`, and `pg_rls_context`, and clears them in `RequestLanguageAndAuditMiddleware` so pooled connections remain clean.

---

## 4. Background Automation & Observability

Scheduled Celery Beat tasks configured in [`backend/config/celery.py`](file:///c:/Users/Administrator/Desktop/Alnahrian/backend/config/celery.py):
- `notifications.sweep_expired_reservations`: every 5 minutes (`*/5`) — expires unpaid/unprepared timed reservations (`reserved_until`) and restores shelf availability.
- `notifications.sweep_expiry_alerts`: daily at `05:00` — generates near-expiry alerts.
- `notifications.process_outbox_events`: every 1 minute (`*`) — leases and dispatches due `OutboxEvent` records with exponential backoff and dead-letter transition.
- `realtime.cleanup_expired_tickets`: every 15 minutes (`*/15`) — prunes expired / consumed `RealtimeTicket` rows older than 1 day.
- `payments.reconcile_online_payments`: every 5 minutes (`*/5`) — polls unsettled gateway transactions.

Operational probes:
- `GET /health/live`: fast process liveness probe (used in Dockerfile `HEALTHCHECK`).
- `GET /health/ready`: executes live DB readiness check.
- `GET /metrics`: protected by `Role.MANAGER` / `Role.ACCOUNTANT` JWT OR `Authorization: Bearer $METRICS_TOKEN` (verified in `perform_authentication` using constant-time `hmac.compare_digest` before JWT rejection).

---

## 5. Host Verification Boundaries (Local Environment Notes)

During Phase 11 verification in this Windows development environment:
- **Verified locally (`LOCALLY_TESTED`)**:
  - Backend: `ruff check .`, `manage.py check`, `manage.py makemigrations --check --dry-run`, double idempotent `seed_demo`, single-`.sql` schema-to-ORM/SQLite parity (`tests/test_schema_sync.py`), API parity across all `107` frontend `httpApi` operations (`tests/test_api_parity.py`), and `pytest -v --cov=apps --cov=integrations --cov-report=term-missing` (**`62 passed, 7 skipped`**, **`91%` statement coverage**).
  - Frontend (`frontend/`): `npm ci --no-audit --no-fund`, `npx next typegen`, `npx tsc --noEmit`, `npm run lint`, `npm run build` (56 routes), and `npm run build:pages` (`STATIC_EXPORT=true`, 48 static routes / 43 entries in `frontend/out/`).
- **Blocked on this Windows host (`BLOCKED` — run in staging CI before DNS cutover)**:
  - Live PostgreSQL (`docker`, `psql`, and `TEST_POSTGRES_URL` unavailable -> all 7 20-thread concurrency tests in [`backend/tests/test_postgres_concurrency.py`](file:///c:/Users/Administrator/Desktop/Alnahrian/backend/tests/test_postgres_concurrency.py) cleanly `@pytest.mark.postgres` skipped)
  - Live Redis, Celery Worker/Beat, multi-worker `channels_redis` WebSocket pub-sub, `docker compose up --build`, live Supabase Storage (`STORAGE_BACKEND=supabase`), and live Qi Card sandbox/production callbacks.
