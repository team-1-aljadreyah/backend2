# Al-Nahrain Wholesale ERP — Backend (`Project-2`)

Production-grade **Python 3.14 + Django 5.2 LTS + Django REST Framework + Django Channels (Daphne ASGI)** modular monolith powering the Al-Nahrain Wholesale Foods Distribution, Warehouse, Accounting, Delivery, and B2B Ordering platform (`/api/v1/*` and `/ws`).

> Designed as a single-codebase modular monolith with dual-engine persistence (**SQLite** for frictionless local development, **PostgreSQL 16+ / Supabase with Row-Level Security** for staging and production), deterministic financial & inventory invariants, and real-time WebSocket fanout backed by a durable transactional outbox.

---

## Table of Contents

1. [Technology Stack](#1-technology-stack)
2. [System Architecture](#2-system-architecture)
3. [Modular Monolith & Bounded Contexts](#3-modular-monolith--bounded-contexts)
4. [Core Operational Workflows](#4-core-operational-workflows)
5. [Engineering Invariants & Security Guarantees](#5-engineering-invariants--security-guarantees)
6. [Local Development Quick Start](#6-local-development-quick-start)
7. [Environment & Configuration Matrix](#7-environment--configuration-matrix)
8. [Testing, Linting, OpenAPI & Capacity Profile](#8-testing-linting-openapi--capacity-profile)
9. [Production Deployment & Operations Runbook](#9-production-deployment--operations-runbook)
10. [Documentation Index](#10-documentation-index)

---

## 1. Technology Stack

| Layer | Technology | Notes |
| :--- | :--- | :--- |
| **Runtime** | Python `3.14` (`>=3.14,<3.15`) | Strict type hints, `Decimal` financial arithmetic |
| **Web / API Framework** | Django `5.2.19` LTS + DRF `3.18.3` | Envelope responses `{success, data, message}`, `APPEND_SLASH = False` |
| **ASGI & Realtime** | Daphne `4.2.3` + Django Channels `4.3.2` | `ProtocolTypeRouter` routing HTTP REST + WebSocket `/ws` |
| **API Schema** | `drf-spectacular` `0.30.0` | OpenAPI 3.0 schema & Swagger UI (`/api/v1/schema/`, `/api/v1/docs/`) |
| **Authentication** | SimpleJWT `5.5.1` + Argon2id (`argon2-cffi`) | Phone-normalized (`07XXXXXXXXX`) login, rotating refresh tokens, `sid` session isolation |
| **Primary Databases** | SQLite (dev) / PostgreSQL 16+ via `psycopg` `3.3.8` (prod) | Unified ORM + [`database/schema.sql`](database/schema.sql) with triggers & RLS |
| **Cache, Pub/Sub & Queue** | Redis `8.1.0` + `channels-redis` `4.3.0` + Celery `5.7.0` | Distributed cache, DRF throttles, WebSocket channel layer, Celery Beat jobs |
| **Media & External APIs** | Pillow `13.0.0`, `httpx` `0.28.1`, `cryptography` `50.0.2`, `pywebpush` | Local/Supabase buckets, Fernet field encryption, Qi Card, ZainCash, Wevlix OTP, VAPID |
| **Tooling** | `uv`, `pytest`, `pytest-cov`, `pytest-asyncio`, `ruff` | Pin-locked dependencies via [`pyproject.toml`](pyproject.toml) & [`uv.lock`](uv.lock) |

---

## 2. System Architecture

The ASGI entrypoint ([`config/asgi.py`](config/asgi.py)) dispatches HTTP traffic to Django REST Framework and WebSocket upgrades (`/ws`) to Django Channels (`apps.realtime`). All domain mutations execute inside atomic transactions with row-level locks (`select_for_update().order_by("id")`), append immutable ledger/audit records, and emit real-time notifications only **after commit** (`transaction.on_commit` + durable `OutboxEvent` fallback).

```mermaid
flowchart TB
    subgraph Clients["Clients (Next.js Static Export / PWA / Driver & Rep Apps)"]
        WEB["Web App & Admin Portal"]
        MOBILE["Sales Rep / Delivery Driver / Shop"]
    end

    subgraph Edge["ASGI Gateway (Daphne / Uvicorn)"]
        PTR["ProtocolTypeRouter (config/asgi.py)"]
        HTTP["Django ASGI + DRF (/api/v1/*, /health/*, /metrics)"]
        WSC["Channels Consumer (/ws)"]
    end

    subgraph Core["Modular Monolith (backend/apps/*)"]
        AUTH["Accounts, Sessions, Profiles & Business Rules Engine"]
        INV["Catalog, Warehouses, FEFO Inventory & Supply Orders"]
        ORD["Orders, Multi-WH Prep, Deliveries & GPS"]
        FIN["Invoices, Payments, FIFO Allocations, Expenses & Commissions"]
        OBS["Reports, Chat, AuditLog, Notifications & Outbox"]
    end

    subgraph Data["State & Async Backbone"]
        DB[("SQLite (Dev) / PostgreSQL + RLS (Prod)")]
        REDIS[("Redis (Cache / Throttles / Channels / Broker)")]
        CELERY["Celery Worker & Beat Schedules"]
    end

    subgraph Ext["External Providers (backend/integrations/*)"]
        STOR["Storage (Local FS / Supabase Buckets)"]
        PAY["Qi Card & ZainCash API v2"]
        OTP["Wevlix OTP (WhatsApp -> Telegram -> SMS)"]
        PUSH["Web Push (VAPID)"]
    end

    WEB --> PTR
    MOBILE --> PTR
    PTR --> HTTP
    PTR --> WSC

    HTTP --> AUTH
    HTTP --> INV
    HTTP --> ORD
    HTTP --> FIN
    HTTP --> OBS

    AUTH --> DB
    INV --> DB
    ORD --> DB
    FIN --> DB
    OBS --> DB

    HTTP --> REDIS
    WSC --> REDIS
    OBS -->|"on_commit / OutboxEvent"| REDIS
    CELERY --> DB
    CELERY --> REDIS

    ORD --> STOR
    INV --> STOR
    FIN --> PAY
    AUTH --> OTP
    OBS --> PUSH
```

### Key Architectural Boundaries

- **Service-Layer Ownership**: Views and serializers remain thin transport adapters; business invariants, lock acquisition ordering, rounding, and audit emission live in each app's `services.py`.
- **Single Authoritative Schema (`database/schema.sql`)**: Django migrations and PostgreSQL views, immutability triggers, and Row-Level Security (RLS) policies stay synchronized and verified by [`tests/test_schema_sync.py`](tests/test_schema_sync.py).
- **Scoped PostgreSQL RLS Context**: [`apps/common/rls.py`](apps/common/rls.py) sets session GUCs (`app.user_id`, `app.role`, `app.active_customer_id`) so PostgreSQL RLS reinforces application-layer RBAC.
- **No Base64 in Database**: Image/video payloads (item photos, receipts, warehouse layouts, private delivery proofs) are validated/decoded via Pillow and persisted to `integrations.storage` (`product-images` public bucket; `delivery-proofs` private bucket served via time-limited signed URLs).

---

## 3. Modular Monolith & Bounded Contexts

```text
backend/
├── config/                 # Split settings (base, development, testing, production), URLs, ASGI/WSGI, Celery
├── apps/                   # 22 domain modules
├── integrations/           # storage, qi_card, zaincash, wevlix adapters
├── database/               # schema.sql (authoritative PostgreSQL DDL, views, triggers, RLS)
├── scripts/                # port80_gateway.mjs (local Node proxy routing Next.js + Daphne)
├── docs/                   # API contract, schema, architecture decisions, readiness & demo reports
├── tests/                  # Cross-app parity, schema sync, security, concurrency & full demo seed suites
├── requirements/           # Exported pip requirements (prod/dev)
├── Dockerfile              # Python 3.14 slim non-root image running Daphne ASGI
└── docker-compose.yml      # PostgreSQL 16 + Redis 7 + Daphne Web + Celery Worker + Celery Beat
```

### App & Integration Map

| Domain Group | Modules | Responsibilities |
| :--- | :--- | :--- |
| **Platform, Auth & Rules** | `apps.common`, `apps.accounts`, `apps.audit` | Envelopes & typed `ErrorCode`s, Iraqi phone normalization, `Asia/Baghdad` timezone helpers, commercial `round_250()`, `IdempotencyKey`, Fernet crypto, `UserSession` (`sid`), Wevlix OTP challenges, permission profiles, custom fields, automation rules, dynamic `settings.advanced` business rules (`SettingsChange` history), append-only `AuditLog`. |
| **Catalog & Warehousing** | `apps.warehouses`, `apps.catalog`, `apps.supply_orders`, `apps.receipts`, `apps.inventory`, `apps.transfers`, `apps.stocktakes` | Warehouses (capacity, GPS, layout, temporary cover staff), multi-tier pricing, 3-day offline `PriceChange` lookup, dated price schedules, clearance pricing, purchase `SupplyOrder` reconciliation (`sent -> partial -> received`), weighted-average-cost `Receipt`s, FEFO `Batch` tracking, lot hold sync, `InventoryMovement` ledger, inter-warehouse `Transfer`s with discrepancy review, category/full `Stocktake`s (`countLocked`). |
| **Customers, Orders & Fleet** | `apps.customers`, `apps.orders`, `apps.deliveries` | Multi-shop `ShopMembership` (`<= 10` shops/account, active shop per device `UserSession`), pending/rejected onboarding, `SpecialPrice`, customer statements, offline order sync (`waiting_stock` split), discount/debt/rule approvals, multi-warehouse `PrepPart` fulfillment, `Vehicle` fleet & shipment splitting, `NAHRAIN:DLV:*` QR dispatch, Fernet+SHA-256 4-digit delivery PIN (5-attempt lockout), private delivery proof photos, `RepLocation` live GPS, shop visits & reorder tracking. |
| **Finance & Reporting** | `apps.invoices`, `apps.payments`, `apps.exchange_rates`, `apps.commissions`, `apps.expenses`, `apps.reports` | Immutable `Invoice` & `InvoiceBatchAllocation` (FEFO COGS trace), `CreditNote`, `ReturnDoc` (`good` restock vs `damaged` + commission clawback), cash/transfer/online `Payment`s, FIFO `PaymentAllocation`, immutable `PaymentReversal`, `Refund`, rep `CashCustody` & handovers, daily `UsdRate`, monthly rep `ClosedCommissionMonth` locks, `Expense` approval workflow, role-redacted KPIs & day summaries. |
| **Realtime & Engagement** | `apps.realtime`, `apps.notifications`, `apps.chat` | Single-use SHA-256 WS tickets (`POST /api/v1/realtime/ticket`), `/ws` consumer with origin regex guard & topic ACLs (`notifications`, `chat`, `orders`, `location`), durable `OutboxEvent` dispatcher, per-user notification read cursors, back-in-stock alerts, support `ChatMessage` threads, VAPID web push subscriptions. |
| **External Integrations** | `integrations.storage`, `integrations.qi_card`, `integrations.zaincash`, `integrations.wevlix` | Local FS / Supabase Storage buckets; Qi Card gateway + HMAC-SHA256 webhook verification; ZainCash API v2 UAT/live JWT init & inquiry; Wevlix OTP multi-channel delivery (`demo` / `sandbox` / `live`). |

---

## 4. Core Operational Workflows

### 4.1 Order-to-Cash, Multi-Warehouse Split & Delivery Verification

Orders enforce configurable dynamic rules (`settings.advanced`: discount limits, debt ceiling behavior, minimum shop order, offline max age, auto-confirm for reps) and reserve FEFO batches until `reserved_until` expires. Financial **Invoices** are only emitted upon actual delivery (`partial` or `full`), never speculatively at checkout.

```mermaid
flowchart LR
    C["Shop / Rep Places Order (POST /orders)"] -->|"Offline > 0d? Check 3d price_changes; Check rules (min_order, debt_limit, discount)"| V{"Needs Manager Approval?"}
    V -->|"Over debt or discount > 3%"| APP["Pending Approval -> Manager Approve"]
    V -->|"Within policy / Auto-confirm rep"| RES["Reserve FEFO Batches (select_for_update + reserved_until)"]
    APP --> RES

    RES --> ACC["Keeper / Staff Accepts Order -> Prep Plan (Single or Multi-WH Split)"]
    ACC --> PREP["Prepare Parts + Pick Carrier/Vehicle (carrier_pick)"]
    PREP --> SHIP["Create Shipments (optional driver_id per load) & Generate QR (NAHRAIN:DLV:*)"]
    SHIP --> DISP["Driver Scans QR -> En Route (GPS stream via WS/REST)"]

    DISP --> PIN["Verify Shop 4-Digit PIN (max 5 tries -> persistent lockout) + Optional Photo + Door Payment"]
    PIN -->|"Full or Partial (rest: keep or cancel)"| INV["Emit Immutable Invoice + InvoiceBatchAllocation (FEFO COGS)"]
    INV --> PAY["Cash / Transfer / Qi Card / ZainCash -> FIFO PaymentAllocation -> Rep Commission"]
```

### 4.2 Procurement, FEFO Stock & Inter-Warehouse Transfers

Inventory quantities are derived from non-expired, unheld FEFO `Batch` rows guarded by both service-layer row locks and database `CHECK (qty_left >= 0)` constraints.

```mermaid
flowchart LR
    PO["SupplyOrder (sent)"] -->|"POST /receipts (optional supply_order_id)"| REC["Inbound Receipt -> Weighted Avg Cost Update -> New FEFO Batch"]
    REC -->|"Reconciles qty"| PO2["SupplyOrder -> partial / received"]
    REC --> TRF["Inter-Warehouse Transfer Dispatch (preserves origin_batch_id & expiry)"]
    TRF -->|"Receiving WH confirms"| RCV{"Quantities Match?"}
    RCV -->|"Yes"| DONE["Target Batch Credited + InventoryMovement Logged"]
    RCV -->|"Shortage / Damage"| REV["Discrepancy Held -> Manager Review & Adjustment"]
    DONE --> STK["Stocktake (countLocked blocks shelf mutations) -> Close Adjustment & DamagedInventory"]
```

### 4.3 Authentication, Wevlix OTP & Real-Time WebSocket Fanout

Multi-shop owners can operate different active shops concurrently across devices because each login issues a `UserSession` (`sid` UUID claim inside the JWT), isolating `active_customer_id` per session rather than overwriting global user state.

```mermaid
sequenceDiagram
    autonumber
    participant Client
    participant API as DRF API (/api/v1)
    participant OTP as Wevlix Gateway
    participant DB as PostgreSQL / SQLite
    participant Outbox as Outbox / Channels
    participant WS as Daphne WS (/ws)

    Note over Client,OTP: Registration, Password Reset & Phone Change
    Client->>API: POST /otp/send {phone, purpose}
    API->>OTP: Send 6-digit code (60s cooldown, 5/hr, 5 wrong tries)
    Client->>API: POST /otp/verify {request_id, code}
    API-->>Client: {otp_token (15m, single-use)}

    Note over Client,WS: Session-Scoped Auth & Secure Realtime Handshake
    Client->>API: POST /auth/login {phone, password}
    API->>DB: Verify Argon2id + create UserSession(sid)
    API-->>Client: JWT access (with sid) + refresh
    Client->>API: POST /realtime/ticket
    API->>DB: Store SHA-256(ticket), TTL 60s
    API-->>Client: {ticket}
    Client->>WS: Connect WS /ws?ticket=... (Origin checked against CORS regex)
    WS->>DB: Atomic single-use ticket consumption & topic ACL check
    WS-->>Client: Subscribed (notifications, chat, orders, location)

    Note over API,WS: Reliable Domain Broadcast
    Client->>API: Mutating Request (e.g. POST /orders)
    API->>DB: Atomic commit + AuditLog + OutboxEvent
    DB-->>Outbox: transaction.on_commit -> Immediate group_send (or 1m Celery Outbox retry)
    Outbox-->>WS: Push JSON event to authorized role/wh/shop/user groups
```

---

## 5. Engineering Invariants & Security Guarantees

1. **Exact Money & Commercial 250 IQD Rounding**:
   - All IQD values use integer / `Decimal` math (never floating-point).
   - [`round_250()`](apps/common/utils.py) implements `Decimal` `ROUND_HALF_UP` to the nearest configurable rounding unit (default `250` IQD, customizable via `settings.advanced.round_to`), matching frontend `Math.round(raw / 250) * 250`.
2. **Zero Negative Stock & Deadlock-Free Locking**:
   - Batch allocations query `.select_for_update().order_by("id")` deterministically inside `@transaction.atomic` and enforce DB `CheckConstraint(qty_left__gte=0)`. Expired (`expiry_date < local_today` in `Asia/Baghdad`) and held lots are excluded from sellable supply.
3. **Strict Server-Side Field Redaction & Profile Limits**:
   - **Costs** (`avg_cost`, `purchase_price`, `total_cost`): omitted outside `{manager, accountant, warehouse}`.
   - **Profits** (`cost`, `profit`, `month_profit`): omitted outside `{manager, accountant}` or when permission profile specifies `limits.no_profit`.
   - **Debt & Payment Details** (`debt_limit`, `balance`, invoice `paid`/`remaining`): redacted for `{warehouse, staff}` or `limits.no_debt`.
   - **Delivery PIN**: encrypted at rest with Fernet (`pin_encrypted`) + `pin_hash` (SHA-256); plaintext PIN is returned **only** to the owning `shop` user. Wrong PIN attempts persist across failures (`403`/lockout at `pin_tries`, default `5`).
4. **Dynamic Business Rules Engine (`apps/accounts/business_rules.py`)**:
   - Runtime settings (`PATCH /api/v1/settings` with `advanced` diffing and immutable `GET /api/v1/settings/history` audit log) control discount bounds (`3%` staff / `30%` max), `min_shop_order`, `over_limit` (`approval` / `block` / `allow`), `shop_edit_minutes`, `offline_max_days`, `auto_confirm_rep_orders`, `custody_cap`, reversal age/role locks (`TOO_OLD`), login lockout (`423 LOCKED`), feature toggles (`403 FEATURE_OFF`), maintenance mode (`503 MAINTENANCE`), and `notify_matrix` role routing.
   - Public `GET /api/v1/site` exposes non-sensitive live bootstrap config (`self_signup`, `features`, `maintenance`, `announcement`, `password_min`, `names`, `users_page`) without authentication.
5. **Idempotency & Fail-Closed Integrations**:
   - Sensitive mutations (`orders`, `payments`, `online-payments`, `transfers`, `returns`) deduplicate via both `Idempotency-Key` header (`IdempotencyKey`) and `client_ref`.
   - Payment webhooks (`integrations/qi_card`) enforce HMAC-SHA256 verification and fail closed (`QI_CARD_MODE=disabled` rejects live mutations cleanly).

---

## 6. Local Development Quick Start

Run inside `backend/` using the project virtual environment (`Python 3.14` managed by `uv` or `.venv`):

```powershell
# 1. Inspect environment & apply migrations (SQLite default: db.sqlite3)
uv run python manage.py check
uv run python manage.py migrate

# 2. Seed demo data (choose mini OR full synthetic ERP dataset)
uv run python manage.py seed_demo                        # mini default (~few records, password: Demo12345!)
uv run python manage.py seed_demo --profile full --with-media   # 70 users, 96 items, 52 shops + 243 WebP assets
uv run python manage.py validate_demo_data --strict-full        # verify referential integrity & assets

# Useful seeder flags:
#   --dry-run
#   --clear-demo-data --confirm-delete-demo-data

# 3. Start ASGI server (required for both REST + WebSockets /ws)
.\.venv\Scripts\daphne.exe -b 0.0.0.0 -p 8000 config.asgi:application
# Or standard HTTP-only dev server:
uv run python manage.py runserver 0.0.0.0:8000
```

> **Note:** `seed_demo` explicitly refuses to run whenever `DJANGO_ENV=production` or production settings are active.

### Demo Credentials

#### `--profile full` Credentials ([`people_data.py`](apps/common/demo_seed/people_data.py))

| Role | Example Phone(s) | Password |
| :--- | :--- | :--- |
| **Manager** | `07701000001` | `AlNahrain!Mgr#2026` |
| **Accountant** | `07701000011` (`prof_senior_acc`), `07701000012` | `AlNahrain!Acc#2026` |
| **Warehouse Keeper** | `07701000021`, `07701000022`, `07701000023` | `AlNahrain!Wh#2026` |
| **Warehouse Staff** | `07701000031`+ | `AlNahrain!Stf#2026` |
| **Sales / Delivery Rep** | `07701000041`+ | `AlNahrain!Rep#2026` |
| **Shop Owner** | `07701000061`+ | `AlNahrain!Shop#2026` |

#### `--profile mini` (Default)

Assigns `--password` / `DEMO_SEED_PASSWORD` (default `Demo12345!`) to `07701000001` (`manager`), `07701000002` (`accountant`), `07701000003`/`004` (`warehouse`), `07701000005` (`staff`), `07701000006`/`007` (`rep`), and `07701000008` (`shop`).

---

## 7. Environment & Configuration Matrix

Copy [`.env.example`](.env.example) to `.env`. Never commit `.env` or real gateway secrets.

| Category | Variable(s) | Dev Default | Production Requirement |
| :--- | :--- | :--- | :--- |
| **Core Django** | `DJANGO_ENV`, `DJANGO_SECRET_KEY`, `DJANGO_DEBUG`, `DJANGO_ALLOWED_HOSTS` | `development` / `false` | `production`, `>= 32`-char secret, `DEBUG=False`, explicit hosts |
| **CORS / CSRF** | `CORS_ALLOWED_ORIGINS`, `CSRF_TRUSTED_ORIGINS` | `localhost:3000`, `127.0.0.1:3000` | Production frontend origins (`*.ama8.uk` regex also enabled in [`base.py`](config/settings/base.py)) |
| **Encryption** | `FIELD_ENCRYPTION_KEY` | Derived dev fallback | Dedicated Fernet key (`Fernet.generate_key()`) |
| **Database** | `DB_ENGINE`, `SQLITE_PATH`, `DATABASE_URL`, `DB_CONN_MAX_AGE`, `DB_SSL_REQUIRE` | `sqlite` (`db.sqlite3`) | `postgres` (startup-enforced in [`production.py`](config/settings/production.py)), URI with `sslmode=require` |
| **Storage** | `STORAGE_BACKEND`, `MEDIA_ROOT`, `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `SUPABASE_BUCKET_*`, `SIGNED_URL_TTL_SECONDS` | `local` (`media/`) | `supabase` + public `product-images` & private `delivery-proofs` buckets (`900s` signed URLs) |
| **Redis & Celery** | `REDIS_URL`, `CHANNELS_REDIS_URL`, `CELERY_BROKER_URL`, `CELERY_RESULT_BACKEND`, `CELERY_TASK_ALWAYS_EAGER` | `LocMemCache`, `InMemoryChannelLayer`, `memory://`, `true` | Live Redis URLs for distributed cache/throttles, Channels pub-sub & Celery (`eager=false`) |
| **Realtime WS** | `REALTIME_TICKET_TTL_SECONDS`, `REALTIME_MAX_CONNECTIONS_PER_USER`, `REALTIME_MAX_MESSAGE_BYTES`, `REALTIME_RATE_LIMIT_PER_10S` | `60`, `5`, `16384`, `30` | Tuned per fleet size (multi-worker WS requires `CHANNELS_REDIS_URL`) |
| **Qi Card** | `QI_CARD_MODE`, `QI_CARD_BASE_URL`, `QI_CARD_MERCHANT_ID`, `QI_CARD_TERMINAL_ID`, `QI_CARD_API_KEY`, `QI_CARD_WEBHOOK_SECRET`, `QI_CARD_PUBLIC_KEY` | `disabled` | Sandbox/live creds + webhook secret (`sandbox` / `live`) |
| **ZainCash v2** | `ZAINCASH_ENV`, `ZAINCASH_MSISDN`, `ZAINCASH_MERCHANT_ID`, `ZAINCASH_SECRET`, `ZAINCASH_CLIENT_ID`, `ZAINCASH_CLIENT_SECRET` | `test` | Live merchant/OAuth creds (`https://pg-api-uat.zaincash.iq` for UAT) |
| **Wevlix OTP** | `WEVLIX_MODE`, `WEVLIX_BASE_URL`, `WEVLIX_API_KEY` | `disabled` | `demo` (dev/QA only: surfaces `sandbox_code`), `sandbox`, or `live` |
| **Web Push & Ops** | `VAPID_PUBLIC_KEY`, `VAPID_PRIVATE_KEY`, `VAPID_CLAIMS_EMAIL`, `METRICS_TOKEN`, `API_ENABLE_DOCS`, `LOG_LEVEL` | `API_ENABLE_DOCS=true` | Set strong `METRICS_TOKEN` (`Authorization: Bearer ...`), disable docs if private |

---

## 8. Testing, Linting, OpenAPI & Capacity Profile

### Verification Commands

```powershell
# Static analysis & Django checks
uv run ruff check .
uv run python manage.py check
uv run python manage.py makemigrations --check --dry-run

# Full automated test suite (SQLite + Channels ASGI tests) & coverage
uv run pytest -v --cov=apps --cov=integrations --cov-report=term-missing

# Schema parity check (Django ORM == database/schema.sql)
uv run pytest tests/test_schema_sync.py -v

# Live PostgreSQL 20-thread concurrency & locking suite (requires live PostgreSQL)
$env:TEST_POSTGRES_URL="postgresql://postgres:postgres@localhost:5432/alnahrian_test"
uv run pytest -m postgres -v

# OpenAPI 3.0 contract generation & validation
uv run python manage.py spectacular --file openapi.yml --validate
```

Latest verified local baseline on SQLite + ASGI (`config.settings.testing`, without `TEST_POSTGRES_URL`): **`95 passed, 7 skipped`** (all 7 skipped tests are `@pytest.mark.postgres` concurrency probes gated on `TEST_POSTGRES_URL`).

### Observed Stress Test & Scaling Profile

Benchmarked on a 4-vCPU (`AMD EPYC 7763`, `8.0 GiB` RAM) host against an isolated clone of the seeded database:

| Configuration | Read Ceiling | Write (`POST /orders`) Ceiling | WebSocket (`/ws`) | Primary Bottleneck |
| :--- | :--- | :--- | :--- | :--- |
| **Default Dev Stack** *(1-worker Daphne + SQLite `DELETE`)* | `/health`: **260.2 RPS**<br>`/site`: **125.1 RPS**<br>`/items`: **17.1 RPS**<br>`/dashboard`: **8.4 RPS**<br>`/orders`: **2.0 RPS** *(fails at `c=50`)* | **3.7 RPS** sequential (`c=1`)<br>**6.4 RPS** (`WAL`, `c=1`)<br>Contends at **`c >= 2`** (`database is locked`) | **500 concurrent sockets** (`0` failures; drops between `500–750`) | 1) SQLite global write lock on concurrent mutations<br>2) `GET /orders` N+1 (`852` SQL queries/req)<br>3) Single Daphne process |
| **Multi-Worker (`uvicorn --workers 4` + `WAL`)** | `/health`: **715.1 RPS** *(2.75x)*<br>`/items`: **52.5 RPS** *(3.07x)* | Requires PostgreSQL for true multi-writer row locking (`select_for_update`) | Requires `CHANNELS_REDIS_URL` across workers | File-level SQLite write lock eliminated once `DB_ENGINE=postgres` |
| **Default DRF Rate Limits** ([`base.py`](config/settings/base.py)) | `120/min` (`2 RPS`) anon/IP<br>`600/min` (`10 RPS`) per user | `10/min` on `login` / `register` | `5` conns/user (`30` msgs/`10s`) | Intentional abuse protection (verified via `429` burst tests) |

---

## 9. Production Deployment & Operations Runbook

### Docker Compose Stack ([`docker-compose.yml`](docker-compose.yml))

Spins up `db` (`postgres:16-alpine`), `redis` (`redis:7-alpine`), `web` (`migrate --noinput` + `daphne -b 0.0.0.0 -p 8000 config.asgi:application`), `worker` (`celery -A config worker -l INFO`), and `beat` (`celery -A config beat -l INFO`):

```bash
docker compose up -d --build
```

For Windows/bare-metal staging where Next.js static/Node (`127.0.0.1:3100`) and Daphne (`0.0.0.0:8000`) run side-by-side behind a single public port, [`scripts/port80_gateway.mjs`](scripts/port80_gateway.mjs) proxies `/api/*`, `/ws`, `/media/*`, and `/health*` to Daphne while serving the frontend everywhere else.

### Celery Beat Scheduled Jobs ([`config/celery.py`](config/celery.py))

| Task | Schedule | Purpose |
| :--- | :--- | :--- |
| `notifications.process_outbox_events` | Every 1 min (`* * * * *`) | Retries unacknowledged durable WS/push outbox events (60s lease, max 5 attempts) |
| `notifications.sweep_expired_reservations` | Every 5 min (`*/5 * * * *`) | Releases expired `reserved_until` inventory locks on unfulfilled orders |
| `payments.reconcile_online_payments` | Every 5 min (`*/5 * * * *`) | Reconciles pending Qi Card / ZainCash online payment intents |
| `realtime.cleanup_expired_tickets` | Every 15 min (`*/15 * * * *`) | Purges expired single-use `RealtimeTicket` rows |
| `notifications.sweep_expiry_alerts` | Daily 05:00 UTC | Emits near-expiry batch notifications across warehouses |

### Observability & Production Checklist

- **Probes**: `GET /health/live` (process alive), `GET /health/ready` (verifies database + cache connectivity), `GET /metrics` (requires manager JWT or `Authorization: Bearer $METRICS_TOKEN`).
- **Hardening**: Keep `DJANGO_ENV=production` (`DEBUG=False`, HSTS preload, `X-Frame-Options: DENY`, secure cookies enforced automatically by [`config/settings/production.py`](config/settings/production.py)).
- **Backups**: Snapshot PostgreSQL (`pg_dump` / Supabase PITR) before every schema migration; rotate `DJANGO_SECRET_KEY`, `FIELD_ENCRYPTION_KEY`, `METRICS_TOKEN`, and gateway secrets out-of-band.

---

## 10. Documentation Index

| Document | Contents |
| :--- | :--- |
| [`docs/PROJECT_CONTEXT.md`](docs/PROJECT_CONTEXT.md) | High-level system architecture, module inventory & security guarantees |
| [`docs/API_CONTRACT.md`](docs/API_CONTRACT.md) | Endpoint contracts, request/response envelopes & error codes |
| [`docs/DATABASE_SCHEMA.md`](docs/DATABASE_SCHEMA.md) | Ledger design, constraints & [`database/schema.sql`](database/schema.sql) overview |
| [`docs/REALTIME_ARCHITECTURE.md`](docs/REALTIME_ARCHITECTURE.md) | WebSocket authentication, topic ACLs, rate limiting & outbox lifecycle |
| [`docs/ARCHITECTURE_DECISIONS.md`](docs/ARCHITECTURE_DECISIONS.md) | ADRs explaining key design trade-offs |
| [`docs/IMPLEMENTATION_STATUS.md`](docs/IMPLEMENTATION_STATUS.md) | Phase-by-phase build log & local vs blocked verification boundaries |
| [`docs/PRODUCTION_READINESS.md`](docs/PRODUCTION_READINESS.md) | Pre-launch checklist & operational readiness gates |
| [`docs/DEMO_DATA_REPORT.md`](docs/DEMO_DATA_REPORT.md) | Synthetic demo seeder specification & asset manifest audit |
