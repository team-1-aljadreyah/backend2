# Al-Nahrain Wholesale ERP — Known Gaps & Environment Boundaries

> Explicit record of schema constraints, host environment boundaries, and verification limits observed during full demo population and external port 80 deployment.

---

## 1. Environment & Infrastructure Boundaries (`BLOCKED` on Windows Host)

1. **PostgreSQL & Live Supabase (`DB_ENGINE=postgres`)**:
   - The host lacks `psql`, a local PostgreSQL server, Docker Engine, and remote `DATABASE_URL` / Supabase Storage credentials.
   - Runtime execution uses SQLite (`DB_ENGINE=sqlite`, `SQLITE_PATH=db.sqlite3`).
   - All `7` multi-threaded concurrency tests in [`backend/tests/test_postgres_concurrency.py`](file:///c:/Users/Administrator/Desktop/Alnahrian/backend/tests/test_postgres_concurrency.py) were **skipped** (`TEST_POSTGRES_URL is not set`).
   - Live PostgreSQL trigger, view, and RLS execution against [`backend/database/schema.sql`](file:///c:/Users/Administrator/Desktop/Alnahrian/backend/database/schema.sql) requires running on staging/production PostgreSQL (where [`backend/apps/common/demo_seed/registry.py`](file:///c:/Users/Administrator/Desktop/Alnahrian/backend/apps/common/demo_seed/registry.py) also enforces a safeguard refusing `clear_demo_data` against immutable ledger triggers).

2. **Redis, Celery Workers & Multi-Worker `channels_redis`**:
   - Live Redis is absent on the Windows host; Celery tasks run eagerly (`CELERY_TASK_ALWAYS_EAGER=true`), caching uses `LocMemCache`, and Channels WebSockets run on single-process `InMemoryChannelLayer` inside Daphne.

3. **External Gateways & Cloud Deployments**:
   - **Qi Card (`QI_CARD_MODE=disabled`)**: Live Qi Card API calls are disabled; seeded settled `OnlinePayment` rows represent simulated sandbox records without fabricating external webhook approvals.
   - **Deployed `https://aln.ama8.uk/` Bundle**: The externally hosted Cloudflare Pages bundle compiles with its own build-time environment variables and mixed-content browser restrictions apply if an HTTPS frontend attempts to call plain `http://5.231.252.96`. Full same-origin live operation is provided at **`http://5.231.252.96`** on port 80 (HTTPS termination on port 443 requires a domain TLS reverse proxy / Cloudflare Tunnel).

4. **Interactive Chrome DevTools Browser Attach**:
   - `chrome_devtools` MCP could not attach (`DevToolsActivePort` was not enabled on the user's running desktop Chrome instance), so automated visual DOM inspection via Chrome DevTools did not run; live port 80 UI routes, auth/RBAC endpoints, signed media responses, and WebSocket handshakes were verified programmatically instead.

---

## 2. Schema Mapping Decisions (Preserving Frontend Contract Parity)

Requested ERP concepts that do not map to dedicated Django tables were mapped onto supported fields rather than inventing breaking migrations:
- **Branches / Locations**: Represented via the `5` regional `Warehouse` records (Karbala Main, Karbala Industrial, Najaf, Baghdad, Hillah/Babil) with GPS coordinates (`lat`/`lng`), capacities, and `layout` metadata.
- **Warehouse / Customer / Vehicle Multi-Photo Galleries & `User.email` / `name_en`**: Not standalone columns in current Django models/API contracts; company logo, item catalog WebPs, user portraits, signed delivery proofs, and expense bills populate all storage-backed image fields (`SiteConfig.brand_logo_path`, `Item.image_path`, `User.photo_path`, `Delivery.photo_path`, `Expense.photo_path`).
- **`Item.sku` / `Brand` / Warehouse Metadata**: Represented via supported `SiteConfig` custom field definitions (`CustomFieldValue`) alongside core barcodes and categories.
