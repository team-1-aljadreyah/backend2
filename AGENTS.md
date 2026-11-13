# Al-Nahrain Wholesale ERP (`backend/`) — Engineering Rules & Architecture Guide

> **Scope**: This file applies to `backend/` (Python 3.14, Django 5.2 LTS, Django REST Framework, Django Channels, Celery). Do **not** modify `frontend/` beyond minimal realtime ticket alignment in `frontend/lib/realtime.ts`, and never modify unrelated directories or parent repositories.

---

## 1. Authoritative Architecture
- **Modular Monolith** under `backend/apps/*` + `backend/integrations/*` + `backend/config/*`.
- **Primary HTTP Contract**: `/api/v1/...` defined by `frontend/lib/api/http.ts`, `frontend/lib/api/types.ts`, `frontend/lib/api/mock.ts`, `frontend/lib/api/mock/rules.ts`, and `frontend/components/nav.ts`.
- **Real-Time WebSocket Contract**: `/ws?ticket=<TICKET>` served via Django Channels ASGI (`config/asgi.py`), authenticated exclusively via single-use SHA-256-hashed tickets from `POST /api/v1/realtime/ticket` (JWTs are **never** placed in WebSocket query strings).
- **Single Unified PostgreSQL Schema**: `backend/database/schema.sql` is the **ONLY** `.sql` file in the entire repository. Every PostgreSQL table, index, `CHECK` constraint, partial unique index, trigger, analytical view, and RLS policy lives strictly in `backend/database/schema.sql` and stays synchronized with Django ORM models (enforced by automated schema-drift tests). SQLite (`DB_ENGINE=sqlite`) remains fully functional for local dev and unit tests.

---

## 2. Backend Module Layout
- `config/` — Split settings (`base.py`, `development.py`, `testing.py`, `production.py`), URLconf (`urls.py`), ASGI (`asgi.py`), WSGI (`wsgi.py`), Celery (`celery.py`).
- `database/schema.sql` — Single idempotent PostgreSQL 15+ / Supabase schema file (no other `.sql` files anywhere).
- `apps/common/` — Response envelope (`{success, data, message}`), typed `ErrorCode`, Iraqi phone normalization (`07XXXXXXXXX`), `Asia/Baghdad` calendar helpers, commercial `round_250`, `IdempotencyKey`, media serving, health/metrics, `seed_demo`.
- `apps/accounts/` — `User`, `UserSession` (per-session active shop isolation), `PriceTier`, `SystemSetting`, `RevokedAccessToken`, JWT + `/auth/*`, `/me`, `/users`, `/lookups`, `/settings`, `/price-tiers`.
- `apps/warehouses/` — `Warehouse` (`name`, `name_en`, `code`, `capacity`, `lat`, `lng`, `active`).
- `apps/catalog/` — `Category`, `Item` (includes `commission_percent`, `image`), `ItemPrice`, `PriceChange`.
- `apps/inventory/` — `Batch` (FEFO, cross-warehouse lot holds), `InventoryMovement`, `DamagedInventory`.
- `apps/supply_orders/` — Inbound supplier purchase orders (`SupplyOrder`, `SupplyOrderLine`, `/supply-orders*`), partial/full receipt linkage (`supply_order_id`).
- `apps/receipts/` — Supplier receipts (`Receipt`, `ReceiptLine`), weighted-average cost update, optional `supply_order_id` fulfillment.
- `apps/transfers/` — Inter-warehouse `Transfer` with source batch trace, partial receive & manager discrepancy review.
- `apps/stocktakes/` — Scoped `Stocktake`, `countLocked`, atomic adjustment & damage recording.
- `apps/customers/` — `Customer`, `ShopAddress`, `ShopMembership`, per-session `/me/shop` & `/me/shops`, `SpecialPrice`, `Statement`, `CreditNote`, `Refund`.
- `apps/orders/` — `Order`, `OrderLine`, `PrepPart`, multi-warehouse prepare, offline sync (`recorded_at`, `waiting_stock`), approval/discount workflows.
- `apps/deliveries/` — QR dispatch (`NAHRAIN:DLV:*`), encrypted + hashed 4-digit PIN with persistent 5-attempt lockout, private proof photos, partial delivery, `RepLocation` GPS tracking (`/deliveries/location`, `/deliveries/locations`).
- `apps/invoices/` — Immutable `Invoice` on delivery, `InvoiceLine`, `InvoiceBatchAllocation` FEFO tracking, `ReturnDoc` & `CreditNote`.
- `apps/payments/` — `Payment`, FIFO `PaymentAllocation`, `PaymentReversal`, `OnlinePayment`, signed Qi Card webhook handler (`WebhookEvent`).
- `apps/commissions/` — Item-weighted collected-payment commission calculation & `ClosedCommissionMonth` snapshots.
- `apps/exchange_rates/` — Daily IQD/USD `UsdRate`.
- `apps/reports/` — Dashboard & analytical reports with strict role redaction.
- `apps/notifications/` — `Notification`, `NotificationRead`, `OutboxEvent` dispatcher + Celery sweeps.
- `apps/chat/` — Shop <-> Staff/Manager support chat (`ChatMessage`, `ChatReadCursor`).
- `apps/audit/` — Append-only `AuditLog`.
- `apps/realtime/` — `RealtimeTicket`, ASGI `ErpRealtimeConsumer`, topic ACLs (`notifications`, `chat`, `orders`, `location`), rate/connection guards, `transaction.on_commit` broadcasters.

---

## 3. Non-Negotiable Engineering Rules
1. **No `trailing_slash` mismatches**: `APPEND_SLASH = False`; routes must match `frontend/lib/api/http.ts` without trailing slashes.
2. **Exact Monetary Math**: Whole IQD amounts use integers / `BigIntegerField` or integer `Decimal`; percentages/FX use `Decimal`. **Never** use `float` in financial calculations. Total rounding uses commercial `round_250`.
3. **Concurrency & No Negative Stock**: Atomic transactions with deterministic `select_for_update()` ordering (`order_by("id")`) + DB `CHECK (qty_left >= 0)`.
4. **Role & Confidentiality Redaction**:
   - `avg_cost`, `purchase_price`, `total_cost`: ONLY `manager`, `accountant`, `warehouse`.
   - `cost`, `profit`, `month_profit`: ONLY `manager`, `accountant`.
   - `debt_limit`, `balance`, `paid`, `remaining`, `debts`: hidden from `warehouse` and `staff`.
   - Delivery `pin`: returned ONLY to owning `shop` user.
   - Support chat office side: `staff` and `manager` only.
   - `staff` role: read-only warehouse access (`stock`, `stocktakes`, `receipts`, `transfers`, `orders`, `deliveries`, `supply-orders`, `damaged`) + full `chat` access (`staff`/`manager`) + count updates in open stocktakes where explicitly permitted.
5. **No Base64 in DB / No Secret Leaks / Honest Testing**: Validate & store images via `StorageBackend`; validate WebSocket tickets with single-use atomic consumption; never claim PostgreSQL/Redis/Docker live tests passed unless executed against live instances.

---

## 4. Verification Commands (`backend/`)
```powershell
.\.venv\Scripts\python.exe manage.py check
.\.venv\Scripts\python.exe manage.py makemigrations --check --dry-run
.\.venv\Scripts\ruff.exe check .
.\.venv\Scripts\pytest.exe -v
.\.venv\Scripts\pytest.exe --cov=apps --cov=integrations
```
