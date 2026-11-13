# Project-2 Backend Implementation & Verification Status

> Authoritative progress tracker for `backend/` (`Python 3.14.10`, `Django 5.2.19`, `DRF 3.18.3`, `Channels 4.3.2`, `Daphne 4.2.3`, `Celery 5.7.0`) and `frontend/` verification (`Next.js 16.4.5`, `React 19.3.0`).

---

## Completed Phases & Evolution Summary

| Phase / Track | Scope & Key Deliverables | Verification Status |
| --- | --- | --- |
| Phases 0–5 | Modular monolith scaffold (`config/`, `apps.common`, `warehouses`, `accounts`, `catalog`, `inventory`, `receipts`, `transfers`, `stocktakes`, `customers`, `orders`), FEFO allocations, 250-IQD rounding, offline 3-day pricing & partial splits, JWT auth, `Idempotency-Key` | `LOCALLY_TESTED` (`pytest` pass) |
| Phases 6–10 | `deliveries` (QR `NAHRAIN:DLV:*`, Fernet-encrypted + SHA-256 5-attempt PIN lock, private storage photos, problems/returns), `invoices` (`InvoiceBatchAllocation`, `CreditNote`, `ReturnDoc`), `payments` (FIFO, reversals, `OnlinePayment` / Qi Card HMAC webhook, refunds), `commissions`, `exchange_rates`, `reports`, `audit`, `notifications` (`OutboxEvent`), `chat`, OpenAPI `/api/v1/schema/` & `/docs/`, `seed_demo` | `LOCALLY_TESTED` (`pytest` pass) |
| Evolution Track 1: Schema & Parity | Unified single DDL [`backend/database/schema.sql`](file:///c:/Users/Administrator/Desktop/Alnahrian/backend/database/schema.sql) (sole `.sql` file in repo: 52 tables, 4 PostgreSQL views, 6 functions, 13 triggers, 37 RLS-enabled tables/policies), `Warehouse` `capacity`/`lat`/`lng`, `tests/test_schema_sync.py` ORM + SQLite column parity | `LOCALLY_TESTED` (`test_schema_sync.py` pass) |
| Evolution Track 2: Supply Orders | Complete `apps.supply_orders` (`GET\|POST /api/v1/supply-orders`, `GET /api/v1/supply-orders/{id}`, `POST /api/v1/supply-orders/{id}/cancel`), keeper warehouse scoping (`404` IDOR guard), automatic `sent -> partial -> received` reconciliation from `POST /api/v1/receipts` | `LOCALLY_TESTED` (`apps/supply_orders/tests.py` pass) |
| Evolution Track 3: Multi-Shop & Live GPS | `UserSession` (`sid` JWT claim) isolating `active_customer_id` per device, `ShopMembership` (`/me/shops`, `/me/shop`, max 10 shops, `clean_shop_address`), `RepLocation` (`POST /api/v1/deliveries/location`, `GET /api/v1/deliveries/locations` with 10-minute freshness & `"out"` shop scoping) | `LOCALLY_TESTED` (`apps/customers/tests.py` pass) |
| Evolution Track 4: Realtime WebSockets | `apps.realtime` (`RealtimeTicket`, `POST /api/v1/realtime/ticket`, `WS /ws` via `ProtocolTypeRouter`), single-use SHA-256 atomic ticket redemption, `Origin` check (`4003`), cap (`4008`), byte/rate limits (`4009`/`4029`/`RATE_LIMITED`), topic ACLs (`user_*`, `role_*`, `wh_*`, `shop_*`, `chat_staff`, `loc_staff`), `transaction.on_commit` broadcast, rep GPS WS fanout, updated `frontend/lib/realtime.ts` & `frontend/docs/realtime.md` | `LOCALLY_TESTED` (`apps/realtime/tests.py` pass) |
| Evolution Track 5: Deployment & Docs | Switched `Dockerfile` & `docker-compose.yml` to Daphne ASGI + `CHANNELS_REDIS_URL`, updated `.env.example`, `pyproject.toml`, and `backend/docs/` suite | `LOCALLY_TESTED` (static config & docs complete; Docker runtime `BLOCKED`) |
| Phase 11: Audit & Remediation | Fixed `MetricsView` `Bearer $METRICS_TOKEN` authentication bug (`apps/common/health.py`), hardened RLS GUC lifecycle (`apps/common/rls.py`), verified durable `OutboxEvent` 60s lease / exponential backoff / 5-attempt dead-letter + 15m `RealtimeTicket` cleanup, expanded [`backend/tests/test_postgres_concurrency.py`](file:///c:/Users/Administrator/Desktop/Alnahrian/backend/tests/test_postgres_concurrency.py) to 7x20-thread concurrency probes (`@pytest.mark.postgres`), added [`backend/tests/test_phase11_remediation.py`](file:///c:/Users/Administrator/Desktop/Alnahrian/backend/tests/test_phase11_remediation.py), and verified full `frontend/` static export build | `LOCALLY_TESTED` (PostgreSQL/Redis/Cloudflare/External creds `BLOCKED` on host) |
| Enterprise Parity Completion | Added full `apps.expenses` app, `SiteConfig` branding/custom fields/permission profiles (`hours`, `read_only`, `deny_actions`, `limits.no_profit`/`no_debt`), `AutomationRule` engine (`RuleRun`), `SecurityHealth` + `LoginAttempt` (`/setup/health`), user soft-delete + self photo/password + `DELETE /me/shops`, warehouse delete/layout/temporary `cover`, `Item.weight_kg` + `quickItem` + `renameCategory`, `Vehicle` fleet & order shipment splitting/loading + parent status sync, `repTargets`, and WS `?token=` query auth matching `frontend/lib/realtime.ts`; synchronized `schema.sql` (`58` tables, `4` views, `6` functions, `17` triggers, `43` RLS policies) + added [`backend/tests/test_enterprise_parity_suite.py`](file:///c:/Users/Administrator/Desktop/Alnahrian/backend/tests/test_enterprise_parity_suite.py) (`6/6` pass) | `LOCALLY_TESTED` (`68 passed, 7 skipped`, `88%` coverage) |
| Full Synthetic Demo Data & Port 80 Network Deployment | Implemented modular [`backend/apps/common/demo_seed/`](file:///c:/Users/Administrator/Desktop/Alnahrian/backend/apps/common/demo_seed/__init__.py) (`catalog_data`, `people_data`, `operations_data`, `media_bind`, `registry`, `runner`), `seed_demo --profile full --with-media --dry-run --clear-demo-data`, `validate_demo_data --strict-full`, `243` SHA-256 verified WebP assets (`seed_assets/asset_manifest.json`), [`backend/tests/test_demo_seed_full.py`](file:///c:/Users/Administrator/Desktop/Alnahrian/backend/tests/test_demo_seed_full.py), Next.js production build + [`backend/scripts/port80_gateway.mjs`](file:///c:/Users/Administrator/Desktop/Alnahrian/backend/scripts/port80_gateway.mjs) routing `http://5.231.252.96` / `ws://5.231.252.96/ws` on Port 80 (`AlNahrain-ERP-HTTP-80` firewall rule), documented in [`DEMO_DATA_REPORT.md`](file:///c:/Users/Administrator/Desktop/Alnahrian/backend/docs/DEMO_DATA_REPORT.md) & [`DEMO_DATA_GAPS.md`](file:///c:/Users/Administrator/Desktop/Alnahrian/backend/docs/DEMO_DATA_GAPS.md) | `LOCALLY_TESTED` (`72 passed, 7 skipped`, `validate_demo_data --strict-full` `PASSED`, Port 80 live) |
| Missing Endpoints, ZainCash, OpenAPI & Security Hardening | Implemented missing endpoints (`GET /catalog` public no-auth active catalog; `POST /counter-sales` keeper sale with FEFO & paid invoice; `DELETE /items/:id` hard/soft delete with 409 guard; `POST /items/:id/video` upload/URL; `POST /orders/:id/rating` 1-5 stars & `GET /reports/delivery-ratings`; `GET/POST/PATCH/DELETE /shop-lists`; `POST /push-subscriptions` & `/delete` with VAPID webpush). Added ZainCash API v2 payment gateway (`provider: "zaincash"`, JWT verification, UAT init & inquiry). Generated 108 drf-spectacular component schemas covering all models & requests. Turned `DEBUG=False` in production with JSON 400/403/404/500 handlers. Whitelisted `aln.ama8.uk`, `api.ama8.uk`, and `5.231.252.96`. Added [`backend/tests/test_missing_endpoints_and_security.py`](file:///c:/Users/Administrator/Desktop/Alnahrian/backend/tests/test_missing_endpoints_and_security.py) (9/9 passed). | `LOCALLY_TESTED` (`77 passed, 7 skipped`, Daphne Port 80 live) |

---

## Executed Verification Commands & Observed Results (Local Host)

### Backend (`c:\Users\Administrator\Desktop\Alnahrian\backend\.venv`)

1. `uv run ruff check .` -> `All checks passed!` (`0` errors)
2. `uv run python manage.py check` -> `System check identified no issues (0 silenced).`
3. `uv run python manage.py makemigrations --check --dry-run` -> `No changes detected`
4. `uv run pytest -v --cov=apps --cov=integrations --cov-report=term-missing` -> **`68 passed, 7 skipped`** in `30.49s`; statement coverage across `apps` & `integrations` = **`88%`** (`11,004` statements, `1,295` missed).
   - **Skipped cases (`7`)** — all in [`backend/tests/test_postgres_concurrency.py`](file:///c:/Users/Administrator/Desktop/Alnahrian/backend/tests/test_postgres_concurrency.py) with reason `TEST_POSTGRES_URL is not set; skipping live PostgreSQL concurrency test.`:
     1. `test_postgres_concurrent_fefo_lock_no_negative_stock`
     2. `test_postgres_concurrent_orders_creation_no_oversell`
     3. `test_postgres_concurrent_transfers_creation_no_oversell`
     4. `test_postgres_concurrent_supply_order_receipt_no_over_receiving`
     5. `test_postgres_concurrent_payments_fifo_allocation`
     6. `test_postgres_concurrent_delivery_dispatch_and_pin_lockout`
     7. `test_postgres_realtime_ticket_single_use_race`

### Frontend (`c:\Users\Administrator\Desktop\Alnahrian\frontend`)

1. `npm ci --no-audit --no-fund` -> exited `0` (`added 482 packages in 2m`)
2. `npx next typegen` -> `TYPEGEN_EXIT=0` (`✓ Types generated successfully`)
3. `npx tsc --noEmit` -> `TSC_EXIT=0` (`0` TypeScript errors)
4. `npm run lint` -> `LINT_EXIT=0` (`eslint` passed cleanly)
5. `npm run build` (`NEXT_PUBLIC_USE_MOCK=false`, `NEXT_PUBLIC_API_URL=http://127.0.0.1:8000`, `NEXT_PUBLIC_WS_URL=ws://127.0.0.1:8000/ws`) -> `BUILD_EXIT=0` (`56` static/prerendered pages)
6. `npm run build:pages` (`cross-env STATIC_EXPORT=true next build`) -> `PAGES_EXIT=0` (`48` static export routes; `frontend/out/index.html` present, `43` top-level entries in `frontend/out/`)

### Classification Boundaries (`BLOCKED` on Windows Host)

- Live PostgreSQL (`psql` / `docker` absent, `TEST_POSTGRES_URL` unset) — all 7 20-thread concurrency tests & live `schema.sql` PG trigger/RLS execution unrun on host
- Live Redis / Celery Worker / Beat / multi-worker `channels_redis` WebSocket pub-sub (`redis-server` absent on Windows host)
- Container runtime (`docker` / `docker compose` absent on Windows host)
- Live Supabase Storage (`STORAGE_BACKEND=supabase`) & live Qi Card payment gateway callbacks (no external credentials; mocked `httpx` tests pass at `94%` / `98%` module coverage)
