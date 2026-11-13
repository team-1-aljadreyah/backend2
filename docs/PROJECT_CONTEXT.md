# Project-2 (Al-Nahrain Wholesale ERP) — System Context

> Last updated after completing backend evolution, PostgreSQL unified schema hardening, Django Channels WebSocket implementation, Supply Orders, Live GPS, Multi-Shop session isolation, and frontend realtime ticket integration.

---

## 1. Repository & Application Layout
- **Frontend (`frontend/`)**: Existing Next.js 16 + React 19 + TypeScript static export deployed to Cloudflare Pages (`NEXT_PUBLIC_WS_URL`, `NEXT_PUBLIC_API_URL`). No visual redesigns or breaking contract changes were made; only `frontend/lib/realtime.ts` and `frontend/docs/realtime.md` were updated to support the secure single-use `POST /api/v1/realtime/ticket` handshake alongside mock mode.
- **Backend (`backend/`)**: Python 3.14 (`3.14.10`) + Django 5.2 LTS (`5.2.19`) + Django REST Framework (`3.18.3`) + Django Channels (`4.3.2`) + Daphne ASGI (`4.2.3`) + Celery (`5.7.0`) modular monolith exposing `/api/v1/*`, `/ws`, `/health/live`, `/health/ready`, `/metrics`, and OpenAPI docs (`/api/v1/schema/`, `/api/v1/docs/`).
- **Database Schema (`backend/database/schema.sql`)**: Single authoritative PostgreSQL 15+ / Supabase DDL file matching Django ORM models 1:1 (including views, immutability & `updated_at` triggers, and RLS policies), verified by `backend/tests/test_schema_sync.py`. Local development uses SQLite (`DB_ENGINE=sqlite`); staging/production uses PostgreSQL (`DB_ENGINE=postgres`).

---

## 2. Backend App Architecture (`backend/apps/` & `backend/integrations/`)
| App / Package | Core Responsibilities |
| --- | --- |
| `apps.common` | JSON envelopes (`{success, data, message}`), typed `ErrorCode` exceptions, Iraqi phone normalization (`07XXXXXXXXX`), `Asia/Baghdad` calendar helpers, commercial 250 IQD rounding, `IdempotencyKey`, Fernet crypto & `client_ip()`, PostgreSQL RLS session GUC helpers (`rls.py`), health & `Bearer $METRICS_TOKEN` metrics views, `seed_demo` management command |
| `apps.accounts` | `User` (phone login, Argon2id, `deleted`/`weak_password`/`profile_id`/`sales_target`), `SessionAwareJWTAuthentication` + `UserSession` (`sid` claim for concurrent multi-shop isolation), `/site`, `/auth/*`, `/me`, `/me/photo`, `/me/password`, `DELETE /users/{id}`, `/users/{id}/profile`, `/lookups`, `/users`, `/settings`, `/settings/site`, `/setup/health`, `/setup/rule-runs`, `PriceTier`, permission profiles, custom field definitions/values, automation rules engine |
| `apps.warehouses` | `Warehouse` (`name`, `name_en`, `code`, `capacity`, `lat`, `lng`, `layout`, `cover`, `active`), `DELETE /warehouses/{id}`, `/warehouses/{id}/layout`, `/warehouses/{id}/cover` & operational scope helpers (`works_in_warehouse`, `covering_warehouse_ids`) |
| `apps.catalog` | `Category`, `Item` (`weight_kg`), `ItemPrice`, `PriceChange` (3-day historical offline order price lookup), item image storage, `PATCH /items/{id}/quick`, `POST /categories/rename` |
| `apps.supply_orders` | `SupplyOrder`, `SupplyOrderLine`, accountant/manager PO creation, cancellation, keeper scoped visibility (`404` cross-warehouse IDOR guard), automatic reconciliation against `Receipt` (`sent -> partial -> received`) |
| `apps.receipts` | Inbound supplier `Receipt` / `ReceiptLine`, weighted average cost update, FEFO `Batch` creation, optional `supply_order_id` progression |
| `apps.inventory` | `Batch` (expiry, hold sync across sibling lots), append-only `InventoryMovement`, `DamagedInventory`, batch trace |
| `apps.transfers` | Inter-warehouse `Transfer`, atomic FEFO lineage preservation, partial receipt & discrepancy manager review |
| `apps.stocktakes` | Full or category-scoped `Stocktake` with `countLocked` shelf blocking and atomic close reconciliation |
| `apps.customers` | `Customer`, structured `clean_shop_address`, pending shop onboarding & rejection, `ShopMembership` (`GET|POST|DELETE /me/shops`, `POST /me/shop` <= 10 shops), `SpecialPrice`, statements & FIFO allocation preview |
| `apps.orders` | `Order` (`shipment_of`, `shipment_no`, `rule_note`, `custom`) / `OrderLine` / `PrepPart`, reservation expiry (`reserved_until`), offline sync & partial split (`waiting_stock`), 3%/30% discount/debt/rule approvals, multi-warehouse prep, `POST /orders/{id}/shipments` |
| `apps.deliveries` | `Delivery` (`NAHRAIN:DLV:*` QR dispatch, `pin_encrypted` + `pin_hash` 5-attempt persistent lockout, private delivery photo signed URLs, partial delivery `rest="keep"\|"cancel"`, problem & return workflows, `POST /orders/{id}/delivery/load`), `Vehicle` (`POST /vehicles`, `PATCH /vehicles/{id}`), & `RepLocation` live GPS |
| `apps.expenses` | `Expense` (`POST|GET /expenses`, `PATCH /expenses/{id}`, `POST /expenses/{id}/approve|reject`) with `expense_approval_above` manager threshold & dashboard net integration |
| `apps.invoices` | `Invoice`, `InvoiceLine`, `InvoiceBatchAllocation` (FEFO cost & trace), `CreditNote`, `ReturnDoc` (`good` restock vs `damaged` logging, commission base clawback) |
| `apps.payments` | `Payment`, FIFO `PaymentAllocation`, immutable `PaymentReversal`, `Refund`, `OnlinePayment`, `WebhookEvent` |
| `apps.exchange_rates` | Daily IQD/USD `UsdRate` history & broadcast notifications |
| `apps.commissions` | Monthly rep collection commissions & immutable `ClosedCommissionMonth` locks |
| `apps.reports` | Dashboard KPIs (incl. `month_expenses`, `month_net`), `GET /reps/targets`, daily sales, grouped sales (`rep\|item\|warehouse\|customer`), slow-moving, near-expiry, debts with role & profile `no_profit`/`no_debt` redaction |
| `apps.notifications` | `Notification`, per-user `NotificationRead`, `OutboxEvent`, Celery Beat periodic maintenance tasks |
| `apps.chat` | Customer support `ChatMessage` & `ChatReadCursor` |
| `apps.audit` | Append-only bilingual `AuditLog` |
| `apps.realtime` | `RealtimeTicket` single-use SHA-256 WS auth (`POST /api/v1/realtime/ticket`) + `?token=` JWT query handshake compatibility, Channels consumer (`/ws`), topic ACLs (`wh_*` incl. cover warehouses), sliding rate/byte/connection limits, `transaction.on_commit` domain fanout (`notifications`, `chat`, `orders`, `location`) |
| `integrations.storage` | Pluggable `local` vs `supabase` object storage (`product-images` public, `delivery-proofs` private) |
| `integrations.qi_card` | Server-side Qi Card adapter & HMAC-SHA256 webhook verifier (fail-closed when `disabled`) |

---

## 3. Critical Domain & Security Guarantees
1. **No Float Money & Commercial Rounding**: All IQD amounts use exact integers / `Decimal`; `round_250()` uses `ROUND_HALF_UP` to nearest `250` IQD matching frontend behavior.
2. **Confidential Field Redaction**: `cost` / `avg_cost` / `total_cost` omitted outside `{manager, accountant, warehouse}`; `profit` omitted outside `{manager, accountant}`; `debt_limit` / `balance` / `paid` / `remaining` omitted for `warehouse` & `staff`; `delivery.pin` returned ONLY to owning `shop`.
3. **Concurrency & Immutability**: Deterministic `select_for_update()` ordering, `CheckConstraint` guards (`qty_left >= 0`, `total >= 0`, `returned_pieces <= qty_pieces`), `Idempotency-Key` + `client_ref` deduplication, atomic SQL `UPDATE` single-use WS ticket consumption, and DB-level append-only triggers in PostgreSQL.
4. **Honest Host Verification & Full Demo Deployment**: Backend SQLite + ASGI/Channels suite (`72 passed, 7 skipped`, including `tests/test_demo_seed_full.py`), `ruff check .`, `manage.py check`, `makemigrations --check`, `test_schema_sync.py`, `seed_demo --profile full --with-media` (deterministic `1,436` `DemoSeedRecord` entries + `243` SHA-256 verified media assets), `validate_demo_data --strict-full` (`PASSED`), and Next.js production build (`96/96` routes) all passed locally. Live service runs publicly on TCP Port 80 (`http://5.231.252.96`) via `backend/scripts/port80_gateway.mjs` + Daphne (`0.0.0.0:8000`) + Next.js (`127.0.0.1:3100`) with Windows Firewall rules enabled. Live PostgreSQL (`TEST_POSTGRES_URL` unset), Redis/Celery daemons, live Supabase Storage, and live Qi Card APIs remain `BLOCKED` on this Windows host. See [`DEMO_DATA_REPORT.md`](file:///c:/Users/Administrator/Desktop/Alnahrian/backend/docs/DEMO_DATA_REPORT.md) and [`DEMO_DATA_GAPS.md`](file:///c:/Users/Administrator/Desktop/Alnahrian/backend/docs/DEMO_DATA_GAPS.md).
