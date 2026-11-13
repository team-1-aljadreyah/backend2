# Architecture Decisions Record (ADR)

---

## ADR-001: Modular Monolith with Service Layer over Microservices
- **Decision**: Structure `backend/` as a Django 5.2 modular monolith organized into domain apps with explicit `services.py` / `selectors.py` modules, keeping views thin.
- **Reason**: Financial ledgers, FEFO stock deduction, invoice creation, order approvals, and outbox notifications require ACID multi-table database transactions (`@transaction.atomic` + `select_for_update`). Microservices would create distributed transaction hazards with zero scaling need at this stage.

## ADR-002: Trailing-Slash-Free API Compatibility
- **Decision**: Configure Django with `APPEND_SLASH = False` and DRF routers with `trailing_slash=False` on `/api/v1` endpoints, matching `frontend/lib/api/http.ts`.
- **Reason**: `http.ts` calls `/orders/${id}/deliver`, `/auth/login`, `/items`, etc. without trailing slashes; `POST` redirects on trailing slashes break HTTP clients and strip headers/bodies.

## ADR-003: Unwrapped Array Payloads Inside `{ success, data }` Envelope
- **Decision**: All existing `GET` collection endpoints return JSON arrays inside `envelope.data`, matching `http.ts` and `axios.ts`. Pagination parameters (`page`, `page_size`) can be added in the future via query params or metadata headers without breaking array callers.
- **Reason**: `axios.ts` unwraps `res.data.data` directly to components expecting `.map()`, `.filter()`, and `.length` on list results.

## ADR-004: Whole-IQD Integer Arithmetic & Commercial 250-IQD Rounding
- **Decision**: Represent IQD currency amounts as exact integer `BigIntegerField` / `Decimal(..., decimal_places=0)` values; represent percentages and USD rates with `DecimalField`. Compute 250-IQD rounding via `Decimal` half-away-from-zero to match commercial pricing rules and JS `Math.round(x / 250) * 250`.
- **Reason**: Eliminates IEEE-754 floating-point drift across invoices, FIFO allocations, commissions, and balance calculations.

## ADR-005: Baghdad Calendar Date (`Asia/Baghdad`) for Business Cutoffs
- **Decision**: Compute business "today" dates (batch expiration `expiry_date >= today`, daily sales defaults, slow-moving thresholds, commission month assignment) in `Asia/Baghdad` (`UTC+3`), storing timestamps in UTC (`USE_TZ = True`).
- **Reason**: Matches `dayOf()` / Iraqi wholesale operations and avoids midnight-to-03:00 AM UTC off-by-one-day bugs on batch expiries.

## ADR-006: Delivery PIN Security & Persistent Attempt Lockout
- **Decision**: Delivery PINs are generated randomly (4 digits), protected at rest using symmetric authenticated encryption (Fernet derived from `FIELD_ENCRYPTION_KEY` / key helper) rather than one-way hashing solely because the existing shop UX displays the 4-digit PIN to the verified owning `shop` user on order view & push notification; stripped unconditionally from API responses for every other role (including `manager`, `accountant`, `warehouse`, `rep`). Wrong PIN attempts increment `pin_attempts` and set `locked=True` at 5 failures in an explicit committed transaction (via `transaction.atomic()` returning `{ ok: false, left, locked }` instead of raising an exception that rolls back the DB state).
- **Reason**: If wrong PIN returned an unhandled HTTP 4xx exception inside an atomic view wrapper, Django would roll back `pin_attempts` and defeat the 5-attempt brute-force lock!

## ADR-007: Private Proof Media & Signed URLs Instead of Base64 in DB
- **Decision**: Client data-URL uploads (`POST /items/{id}/image`, `POST /orders/{id}/deliver`) are decoded, validated by Pillow (`max dimensions/size`, format whitelist `JPEG/PNG/WEBP`), and written to `StorageBackend` (`LocalMediaStorage` in dev, `SupabaseStorageClient` in production). Databases store only object storage keys (`image_path`, `photo_path`), never Base64 blobs.
  - `product-images` uses public read URLs.
  - `delivery-proofs` is private; `GET /orders/{id}/delivery/photo` enforces `canSeeOrder` authorization and returns a short-lived signed URL (or Django signed expiring proxy URL in local mode) usable cross-origin by `<img src={...}>` from Cloudflare Pages.
- **Reason**: Keeps PostgreSQL fast, prevents multi-megabyte JSON bloat in list endpoints, and keeps customer delivery photos confidential.

## ADR-008: Notification Read Tracking for Both User-Targeted and Role-Broadcast Notifications
- **Decision**: Support direct user notifications AND role-targeted notifications with a `NotificationRead(notification, user)` join table (plus denormalized `read` resolution for the current requester).
- **Reason**: Role alerts (e.g., all managers or all warehouse keepers alerted when a batch is held) must not mark the notification read for User B simply because User A read it.

## ADR-009: Consistent Debt Balance Calculation (Including Credit Notes)
- **Decision**: Define `invoice_remaining = max(0, total - paid - returned - credited)` uniformly across customer balances, FIFO allocation previews, `debts` reports, and credit-limit checks.
- **Reason**: Closes a subtle discrepancy where the mock `debts` report forgot to subtract `credited`.

## ADR-010: Excess Online Payment Settlement Handling
- **Decision**: When a Qi Card `OnlinePayment` settles asynchronously after debt already dropped (e.g. concurrent cash payment), allocate up to unpaid invoices cleanly and preserve any unallocated excess in an explicit `Payment.unallocated_amount` / negative customer ledger credit balance (making it automatically visible on `Customer.balance < 0` and eligible for `Refund` or manual credit allocation, rather than silently losing customer money).
- **Reason**: Financial auditability requires zero unaccounted funds during concurrent online + cash collections.

## ADR-011: Local SQLite Parity & PostgreSQL Authoritative Locking
- **Decision**: Single ORM codebase with `DB_ENGINE=sqlite` (default dev: FK enforcement pragma, timeout=30) and `DB_ENGINE=postgres` (`psycopg` v3, persistent pool / Supabase pooler support). Stock/payment/delivery services use `select_for_update()` (no-op on SQLite single-writer, true row lock on PostgreSQL) and consistent lock acquisition order (`order_by("id")`).
- **Reason**: Enables immediate local development without external dependencies while maintaining ACID serializability in staging/production on PostgreSQL.

## ADR-012: Self-Referencing Counter-Entry for Payment Reversals
- **Decision**: Model payment reversals as immutable negative-amount `Payment` rows linked via `reversal_of = OneToOneField("self")` and `reversed = BooleanField`, preserving original `PaymentAllocation` links alongside inverse balances.
- **Reason**: Keeps FIFO receipt history, customer account statement timelines, commission collections, and cash drawer logs strictly append-only in one canonical ledger without mutating or deleting historical receipts.

## ADR-013: Per-User Notification Read & Support Chat Cursor Isolation
- **Decision**: Track read receipts independently per user via `NotificationRead(notification, user)` and `ChatReadCursor(user, customer, last_read_message)` instead of a single boolean column on `Notification` or `ChatMessage`.
- **Reason**: Role-broadcast alerts (e.g., all managers/warehouse keepers) and shared staff support threads remain unread for other staff until each individual user marks them read.

## ADR-014: Return Commission Base Clawback & Closed-Month Lock
- **Decision**: Store `ReturnDoc.commission_base` (pro-rata portion recovered against the invoice rep's commission) when goods are returned and block `Payment` reversals (`MONTH_CLOSED`) when the payment's Baghdad month (`YYYY-MM`) has an immutable `ClosedCommissionMonth` snapshot.
- **Reason**: Prevents double-deductions or silent retroactive commission distortion once monthly rep payouts are closed.

## ADR-015: Single Unified `backend/database/schema.sql` + Schema Drift Guard
- **Decision**: Maintain **exactly one `.sql` file** across the entire repository (`backend/database/schema.sql`), defining all PostgreSQL 15+ tables, constraints, triggers, analytical views, and RLS policies alongside identical Django 5.2 ORM models and migrations used for both SQLite dev and PostgreSQL production. Enforce 100% column/table parity via `backend/tests/test_schema_sync.py`.
- **Reason**: Prevents schema divergence between SQL DDL deployments (e.g., Supabase) and Django migrations while catching drift automatically in CI.

## ADR-016: Concurrent-Safe Multi-Shop Sessions via `UserSession` (`sid` JWT Claim)
- **Decision**: Store `active_customer_id` per login session in `accounts_user_session` keyed by an unguessable `session_id` (`sid` claim in access & refresh JWTs) instead of mutating a single shared field on `User` or relying on client headers.
- **Reason**: A shop owner logged into Branch A on one device and Branch B on another concurrently keeps independent active shop contexts, and `POST /auth/logout` or user deactivation revokes the specific `UserSession` immediately across REST and WebSockets.

## ADR-017: Fernet-Encrypted + SHA-256 Delivery PIN Storage (`pin_encrypted` + `pin_hash`)
- **Decision**: Replace plain-text PIN storage with constant-time SHA-256 verification (`pin_hash`) paired with reversible symmetric `Fernet` encryption (`pin_encrypted` keyed via `FIELD_ENCRYPTION_KEY`) so only the owning shop receives its active PIN when inspecting its order/notification, while delivery attempts lock persistently at 5 wrong guesses.
- **Reason**: Preserves frontend PIN visibility for the shop owner without exposing plain-text PINs at rest or in unauthorized serializers.

## ADR-018: Short-Lived Single-Use SHA-256 Tickets for WebSocket Handshake (`POST /api/v1/realtime/ticket`)
- **Decision**: Issue opaque 60-second random tickets over authenticated HTTPS `POST /api/v1/realtime/ticket`, persist only their SHA-256 digest in `realtime_ticket`, and redeem them atomically (`UPDATE ... WHERE used_at IS NULL AND expires_at > NOW()`) during WebSocket `/ws?ticket=...` connect rather than placing long-lived JWTs in URL query strings.
- **Reason**: Eliminates JWT leakage in reverse-proxy, load-balancer, or browser history logs while guaranteeing single-use replay protection even under concurrent PostgreSQL connection attempts.

## ADR-019: Durable Transactional Outbox Leasing, Backoff & Dead-Lettering
- **Decision**: Persist realtime wakeup events (`OutboxEvent`) inside the domain DB transaction and wake delivery in `transaction.on_commit(...)` (`_wake_outbox_event`), claiming each row via atomic lease transition (`PENDING` -> `PROCESSING` with `OUTBOX_LEASE_SECONDS = 60`), retrying failures with capped exponential backoff `min(300, 15 * 2^(attempts - 1))`, and halting at `OUTBOX_MAX_ATTEMPTS = 5` (`FAILED`), backed by 1-minute Celery sweep and 15-minute `RealtimeTicket` pruning.
- **Reason**: Prevents phantom WebSocket broadcasts on rolled-back transactions while surviving worker crashes, stale locks, and transient channel-layer outages without dropping domain wakeups.

## ADR-020: Request-Scoped PostgreSQL RLS Session GUCs (`apps/common/rls.py`)
- **Decision**: Bind PostgreSQL session GUCs (`app.user_id`, `app.role`, `app.warehouse_id`, `app.active_customer_id`) on authentication (`JwtAuthentication`), shop switching (`switch_active_shop`), and background tasks (`pg_rls_context`), and clear them unconditionally in `RequestLanguageAndAuditMiddleware` (`clear_pg_session_context`) so 37 RLS-enabled tables in `backend/database/schema.sql` enforce defense-in-depth row isolation across pooled connections.
- **Reason**: Prevents cross-request GUC leakage on pooled PostgreSQL connections while enforcing DB-level multi-tenant and warehouse barriers independently of ORM filters.

## ADR-021: Dual-Auth Scrape Compatibility on `/metrics` (`MetricsView`)
- **Decision**: Inspect `Authorization: Bearer $METRICS_TOKEN` via `hmac.compare_digest` in `MetricsView.perform_authentication` before running default DRF `JWTAuthentication`, while preserving `Role.MANAGER` / `Role.ACCOUNTANT` JWT access for dashboard admins.
- **Reason**: DRF's default `JWTAuthentication` runs before `check_permissions()` and rejects static scrape tokens with `401 Unauthorized` unless short-circuited during authentication.
