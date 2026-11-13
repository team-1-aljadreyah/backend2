# Project-2 ERP Backend (Al-Nahrain Wholesale Foods)

Production-grade Django 5.2 LTS + Django REST Framework modular monolith powering `/api/v1` for the Project-2 Next.js frontend.

## Tech Stack
- **Runtime**: Python 3.14 (`backend/.venv`)
- **Framework**: Django 5.2 LTS, Django REST Framework, `drf-spectacular` (OpenAPI 3.0)
- **Authentication**: Phone-normalized (`07XXXXXXXXX`) Argon2id password hashing + SimpleJWT (`access` + rotating `refresh` + token blacklist + session invalidation)
- **Database**:
  - **Local development**: SQLite (`DB_ENGINE=sqlite`, `PRAGMA foreign_keys=ON`, 30s busy timeout)
  - **Staging & Production**: PostgreSQL / Supabase PostgreSQL (`DB_ENGINE=postgres`, Psycopg 3)
- **Media Storage**: `LocalMediaStorage` (`STORAGE_BACKEND=local`) or Supabase Storage (`STORAGE_BACKEND=supabase`) with public `product-images` bucket and private signed `delivery-proofs` bucket
- **Background Workers**: Celery + Redis + `celery beat` (reservation sweeps, expiry alerts, transactional outbox dispatch, Qi Card online payment reconciliation)

---

## Local Development Quick Start (SQLite)

Run inside `backend/`:

```powershell
# 1. Create / sync environment with uv (or activate existing .venv)
.\.venv\Scripts\python.exe manage.py check
.\.venv\Scripts\python.exe manage.py migrate

# 2. Seed idempotent demo dataset (non-production only)
.\.venv\Scripts\python.exe manage.py seed_demo

# 3. Start API server
.\.venv\Scripts\python.exe manage.py runserver 0.0.0.0:8000
```

### Demo Accounts Created by `seed_demo`
All newly seeded users receive the `--password` argument or `DEMO_SEED_PASSWORD` (default `Demo12345!`):
- Manager: `07701000001`
- Accountant: `07701000002`
- Warehouse Keeper (Karkh / WH-KARKH): `07701000003`
- Warehouse Keeper (Rusafa / WH-RUSAFA): `07701000004`
- Warehouse Staff (WH-KARKH): `07701000005`
- Sales + Delivery Rep: `07701000006`
- Delivery-Only Driver: `07701000007`
- Multi-Shop Owner: `07701000008`

`seed_demo` refuses to run whenever `DJANGO_ENV=production` or `DJANGO_SETTINGS_MODULE=config.settings.production`.

---

## Environment Variables
Copy `.env.example` to `.env` for local overrides. Key variables:

| Variable | Dev Default | Production Requirement |
| --- | --- | --- |
| `DJANGO_ENV` | `development` | `production` |
| `DJANGO_SECRET_KEY` | dev fallback | Strong random string >= 32 chars |
| `FIELD_ENCRYPTION_KEY` | derived dev key | Dedicated Fernet key (`Fernet.generate_key()`) |
| `DB_ENGINE` | `sqlite` | `postgres` (enforced on startup) |
| `DATABASE_URL` | — | Supabase/PostgreSQL URI (`sslmode=require`) |
| `STORAGE_BACKEND` | `local` | `supabase` |
| `SUPABASE_URL` / `SUPABASE_SERVICE_ROLE_KEY` | — | Required when `STORAGE_BACKEND=supabase` |
| `REDIS_URL` / `CELERY_BROKER_URL` | `memory://` | Redis URL for distributed cache, throttles & Celery |
| `QI_CARD_MODE` | `disabled` | Live/sandbox mode only when gateway keys + webhook secret set |
| `METRICS_TOKEN` | — | Bearer token protecting `GET /metrics` in production |
| `CORS_ALLOWED_ORIGINS` | localhost origins | Exact Cloudflare Pages frontend origin(s) |

---

## Testing, Linting & OpenAPI Contract

```powershell
# Django system & migration checks
.\.venv\Scripts\python.exe manage.py check
.\.venv\Scripts\python.exe manage.py makemigrations --check --dry-run

# Lint (ruff)
.\.venv\Scripts\python.exe -m ruff check .

# Test suite + coverage (SQLite suite)
.\.venv\Scripts\python.exe -m pytest -v --cov=apps --cov=integrations

# OpenAPI 3.0 schema validation / export
.\.venv\Scripts\python.exe manage.py spectacular --file openapi.yml --validate

# PostgreSQL row-locking concurrency probe (when PostgreSQL is available)
$env:TEST_POSTGRES_URL="postgresql://user:pass@localhost:5432/alnahrian_test"
.\.venv\Scripts\python.exe -m pytest -m postgres -v
```

Note: Live PostgreSQL concurrency tests (`@pytest.mark.postgres`), live Redis throttling, Docker Compose stacks, and live Qi Card sandbox calls require those external runtimes/credentials to be provisioned; without `TEST_POSTGRES_URL`, the Postgres marker test skips explicitly.

---

## Production Deployment Runbook (Docker & Supabase)

1. Provision **Supabase PostgreSQL**, **Supabase Storage** buckets (`product-images` public read, `delivery-proofs` private), and **Redis**.
2. Configure `.env` with `DJANGO_ENV=production`, `DB_ENGINE=postgres`, `DATABASE_URL`, `STORAGE_BACKEND=supabase`, `FIELD_ENCRYPTION_KEY`, `METRICS_TOKEN`, `CORS_ALLOWED_ORIGINS`, and Qi Card credentials/webhook secret.
3. Build and start the multi-container stack (`web`, `worker`, `beat`, `redis`, and optional local `db`):
   ```bash
   docker compose up -d --build
   ```
   `web` runs `python manage.py migrate --noinput` followed by Gunicorn (`config.wsgi:application`).
   `worker` runs `celery -A config worker -l INFO`.
   `beat` runs `celery -A config beat` on schedules configured in `config/celery.py`:
   - `notifications.sweep_expired_reservations` — every 5 minutes (`*/5 * * * *`)
   - `notifications.sweep_expiry_alerts` — daily at 05:00 UTC
   - `notifications.process_outbox_events` — every minute
   - `payments.reconcile_online_payments` — every 5 minutes (`*/5 * * * *`)
4. **Health & Monitoring Endpoints**:
   - Liveness: `GET /health/live`
   - Readiness (verifies DB & cache): `GET /health/ready`
   - Metrics (manager or `Authorization: Bearer $METRICS_TOKEN`): `GET /metrics`
   - OpenAPI / Docs (when `API_ENABLE_DOCS=true`): `GET /api/v1/schema/` and `GET /api/v1/docs/`
5. **Backups & Security Notes**:
   - Automated daily PostgreSQL snapshots (`pg_dump` or Supabase managed PITR backups) before applying migrations.
   - Rotate `FIELD_ENCRYPTION_KEY`, `DJANGO_SECRET_KEY`, and `SUPABASE_SERVICE_ROLE_KEY` out-of-band; never store credentials or Base64 delivery images in the database.
   - Use Supabase transaction pooler / `CONN_MAX_AGE` settings cleanly, keeping `select_for_update()` strictly inside `@transaction.atomic` with deterministic primary-key ordering (`order_by("id")`).
