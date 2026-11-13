# Al-Nahrain Wholesale ERP — Demo Data & Port 80 Deployment Report

> Generated from observed local SQLite execution (`backend/db.sqlite3`), automated test verification, and live HTTP/WebSocket checks on **`http://5.231.252.96`** (TCP Port 80).

---

## 1. Environment & Network Topology

| Layer | Process / Bind | External Access | Purpose |
| --- | --- | --- | --- |
| Public Gateway | `node scripts/port80_gateway.mjs` on `0.0.0.0:80` | `http://5.231.252.96` / `ws://5.231.252.96/ws` | Routes `/api/*`, `/media/*`, `/health/*`, `/metrics`, `/admin/*`, `/static/*`, `/ws*` to Daphne; routes UI traffic to Next.js |
| Backend ASGI | `daphne -b 0.0.0.0 -p 8000 config.asgi:application` | `http://5.231.252.96:8000` | Django 5.2.19 + DRF 3.18.3 + Django Channels 4.3.2 |
| Frontend Next.js | `next start -H 127.0.0.1 -p 3100` | Served behind Port 80 at `http://5.231.252.96/` | Production build (`NEXT_PUBLIC_USE_MOCK=false`, `NEXT_PUBLIC_API_URL=""`, `NEXT_PUBLIC_WS_URL=ws://5.231.252.96/ws`) |
| Firewall Rules | Windows Defender Firewall Inbound | TCP `80` (`AlNahrain-ERP-HTTP-80`), TCP `8000` (`AlNahrain-ERP-API-8000`) | Profile `Any`, Action `Allow` |
| Database | SQLite (`DB_ENGINE=sqlite`, `SQLITE_PATH=db.sqlite3`) | Local file-backed SQLite | Active runtime DB for full demo environment |
| CORS / Origins | `.env` `CORS_ALLOWED_ORIGINS` & `CSRF_TRUSTED_ORIGINS` | `https://aln.ama8.uk`, `http://5.231.252.96`, `http://localhost` | Supports both Port 80 same-origin and external `https://aln.ama8.uk` preflights |

---

## 2. Full Demo Dataset Summary (`seed_demo --profile full --with-media`)

Executed clean migration followed by `python manage.py seed_demo --profile full --with-media` and strict verification via `python manage.py validate_demo_data --strict-full`:

| Entity / Domain | Verified Count | Key Coverage Highlights |
| --- | --- | --- |
| Warehouses | `5` | Active distribution centers across Karbala (Main & Industrial), Najaf, Baghdad, and Hillah/Babil with shelf layout metadata |
| Price Tiers | `4` | Multi-tier wholesale/retail pricing structure |
| USD Exchange Rates | `40` | 40-day historical curve ending at `1500.00` IQD/USD |
| Users | `70` | `1` Manager, Accountants, Warehouse Keepers (`1` active per warehouse), Staff, Sales Reps, Delivery Reps, Dual-role Rep, Shop Owners, plus disabled/idle accounts for RBAC/lock testing (`LoginAttempt` telemetry populated) |
| Customers / Shops | `52` | Active, pending approval, and rejected Iraqi retail/wholesale shops with structured `address_info`, debt limits, multi-shop `ShopMembership`, and `SpecialPrice` overrides |
| Categories & Items | `96` items (`10` categories) | Iraqi wholesale FMCG items with EAN-13 barcodes, `weight_kg`, tier prices, and 3-day historical `PriceChange` entries for offline sync |
| Inventory Batches | `511` | FEFO expiry spread including near-expiry, expired (unsold/blocked), held, and low-stock items (`0 <= qty_left`) |
| Supply Orders & Receipts | `10` supply orders | Reconciled receipts (`sent -> partial -> received`) linked to supplier purchasing |
| Transfers & Stocktakes | `14` transfers / `5` stocktakes | `sent`/`received`/discrepancy transfers; `4` closed stocktakes + `1` open category-scoped stocktake |
| Orders & Deliveries | `123` orders / `5` vehicles | Delivered orders (`86`), live dispatch (`out`, problem reported `shop_closed`), `ready` (planned & unplanned), `pending`, `needs_approval`, `waiting_stock` (`offline=True` split), and `cancelled` |
| Invoices & Adjustments | `86` invoices / `12` returns / `8` credit notes | `InvoiceBatchAllocation` FEFO lot lineage, zero expired sales, good-condition restocks vs damaged logs |
| Payments & Refunds | `80` payments / `5` refunds | FIFO invoice allocations, `4` immutable `REV-*` reversals, `6` simulated settled `OnlinePayment` records (`OnlinePaymentStatus.SUCCESS`) |
| Expenses & Commissions | `28` expenses / `2` closed months | Approved, rejected, cancelled, and pending expenses across fuel/maintenance/utilities/salaries/rent/hospitality/marketing; 2 locked commission snapshots |
| Support Chat | `18` threads | Multi-turn bilingual conversations with unread cursor states |
| Media & Manifest | `243` manifest assets / `281` checked storage files | `asset_manifest.json` SHA-256 verified (`logo.webp`, item WebPs, user portraits, signed private delivery proofs, expense bills) |

> Demo credentials for manual testing were written locally to gitignored [`backend/seed-output/DEMO_ACCOUNTS.local.md`](file:///c:/Users/Administrator/Desktop/Alnahrian/backend/seed-output/DEMO_ACCOUNTS.local.md).

---

## 3. Executed Checks & Verification Matrix

| Check | Outcome | Observed Result |
| --- | --- | --- |
| `seed_demo --profile full --with-media` (Run 1) | `PASSED` | `"already_seeded": false`, created full ecosystem (`243` media assets) |
| `seed_demo --profile full --with-media` (Run 2) | `PASSED` | `"already_seeded": true`, identical entity counts across all tables |
| `validate_demo_data --strict-full` | `PASSED` | `"status": "PASSED"`, `"failures": []` (`281` media paths readable, `243` manifest hashes valid, ledger arithmetic reconciled) |
| `clear_demo_data` safety test | `PASSED` | Deleted `1,436` `DemoSeedRecord` entries & seeded child rows while preserving non-demo `Category` & `Item` rows |
| `ruff check .` | `PASSED` | `All checks passed!` (`0` errors) |
| `python manage.py check` | `PASSED` | `System check identified no issues (0 silenced).` |
| `python manage.py makemigrations --check --dry-run` | `PASSED` | `No changes detected` |
| `pytest` (full backend suite) | `PASSED` (`72 passed, 7 skipped`) | Includes `tests/test_demo_seed_full.py` (`4/4` pass) & `test_schema_sync.py`; `7` PostgreSQL concurrency tests skipped (`TEST_POSTGRES_URL` unset) |
| Next.js `npm run build` | `PASSED` | Compiled in `19.3s`, `0` TypeScript errors, `96/96` routes generated |
| Live Port 80 UI check (`http://5.231.252.96`) | `PASSED` (`17/17`) | `/`, `/login`, `/dashboard`, `/items`, `/stock`, `/orders`, `/deliveries`, `/invoices`, `/payments`, `/expenses`, `/support`, `/map`, `/settings/advanced`, `/hall`, `/rep`, `/driver`, `/client` returned `HTTP 200` |
| Live Port 80 API & RBAC check | `PASSED` (`6/6` roles) | Login & endpoint checks HTTP `200` for `manager`, `accountant`, `warehouse`, `rep_sales`, `rep_delivery`, and `shop` |
| Live Port 80 Media & Signed Proof check | `PASSED` | `logo.webp` (`6,704` bytes), item WebP (`7,586` bytes), signed `/api/v1/media/signed` proof (`7,130` bytes) returned `HTTP 200` |
| Live Port 80 WebSocket check | `PASSED` | `ws://5.231.252.96/ws?token=...` connected and subscribed cleanly (`WS_OPEN_AND_SUBSCRIBED`) |
| CORS preflight (`https://aln.ama8.uk`) | `PASSED` | `OPTIONS /api/v1/auth/login` -> `HTTP 200` with `Access-Control-Allow-Origin: https://aln.ama8.uk` |
