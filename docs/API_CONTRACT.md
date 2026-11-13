# API Contract — `/api/v1`

> **Source Compatibility**: Matches `frontend/lib/api/http.ts`, `frontend/lib/api/axios.ts`, `frontend/lib/api/errors.ts`, `frontend/lib/api/types.ts`, and `frontend/lib/api/mock.ts`.

---

## 1. Global Protocol Conventions
- **Base URL**: `/api/v1`
- **Trailing Slashes**: Not required (`APPEND_SLASH = False`, DRF `trailing_slash=False`); routes must accept exact frontend paths without `301` redirects.
- **Request Headers**:
  - `Authorization: Bearer <access_token>` (omitted on `/auth/login`, `/auth/register`, `/auth/refresh`)
  - `Accept-Language: ar | en` (determines error message language)
  - `Idempotency-Key: <string>` (on mutations accepting deduplication)
- **Success Response Envelope**:
  ```json
  { "success": true, "data": <T>, "message": "optional localized text" }
  ```
- **Error Response Envelope**:
  ```json
  { "success": false, "message": "<localized message>", "code": "<ErrorCode>", "errors": { "<field>": "<localized error>" } }
  ```
- **Typed Error Codes (`ErrorCode`)**:
  `VALIDATION`, `UNAUTHENTICATED`, `FORBIDDEN`, `NOT_FOUND`, `OUT_OF_STOCK`, `DEBT_LIMIT`, `RETURN_TOO_MUCH`, `ALREADY_REVERSED`, `MONTH_CLOSED`, `BAD_STATE`, `NETWORK`.
- **List Responses**: Returned directly as JSON arrays in `data` (never nested under `{ results, count }`) unless opt-in query/header pagination is explicitly requested.
- **Monetary Representation**: All IQD amounts are whole integers; percentages and USD exchange rates are numeric (`Decimal` in Django, serialized cleanly to JSON numbers where TS `number` expects them).

---

## 2. Endpoint Inventory

### A. Auth & Session
| Method | Path | Auth / Roles | Input | Response (`data`) | Key Errors & Rules |
| --- | --- | --- | --- | --- | --- |
| POST | `/auth/login` | Public | `{ phone, password }` | `Session { token, refresh?, user }` | Normalizes Iraqi phone; checks active status; Argon2 verify; rate-limited |
| POST | `/auth/register` | Public | `RegisterInput` | `Session { token, refresh?, user }` | Validates phone unique, password >= 6, full address card; creates `shop` user + `Customer(status="pending")` |
| POST | `/auth/logout` | Auth | `{ refresh? }` | `{ ok: true }` | Blacklists refresh/session token cleanly |
| POST | `/auth/refresh` | Public | `{ refresh }` | `{ token, refresh }` | Issues rotated access token |
| GET | `/me` | Auth | — | `User` | Resolves active `customer_id` for `shop` users |
| GET | `/lookups` | Auth | — | `Lookups { warehouses, tiers, categories, settings, active_order_rules }` | Role-safe reference catalog |

### B. Users, Settings & Price Tiers
| Method | Path | Auth / Roles | Input | Response (`data`) | Key Errors & Rules |
| --- | --- | --- | --- | --- | --- |
| GET | `/users` | `manager`, `accountant` | — | `User[]` | Excludes password hash |
| POST | `/users` | `manager` | `UserInput` | `User` | Enforces phone uniqueness; max 1 active `warehouse` keeper per `warehouse_id` |
| PATCH | `/users/{id}` | `manager` | Partial `UserInput` | `User` | Same keeper uniqueness + password hash if provided |
| PATCH | `/settings` | `manager` | Partial `Settings` | `Settings` | Updates `reservation_hours`, `near_expiry_days`, `commission_pct`, `usd_rate`, etc. |
| POST | `/price-tiers` | `manager` | `{ name, name_en }` | `PriceTier` | Creates tier & initializes default prices if needed |
| PATCH | `/price-tiers/{id}` | `manager` | `{ name?, name_en? }` | `PriceTier` | Renames tier |

### C. Catalog, Stock, Batches & Damaged Inventory
| Method | Path | Auth / Roles | Input | Response (`data`) | Key Errors & Rules |
| --- | --- | --- | --- | --- | --- |
| GET | `/items` | Auth | — | `Item[]` | `avg_cost` redacted unless `[manager, accountant, warehouse]`; `shop` sees only its effective price tier; rep filtered? (`rep` sees catalog items, restricted at order if `rep_categories`) |
| GET | `/items/{id}` | Auth | — | `Item` | Same role redactions |
| POST | `/items` | `manager` | `NewItem` | `Item` | Validates unique SKU/name rules, `pieces_per_carton >= 1`, writes `ItemPrice` & `PriceChange` |
| PATCH | `/items/{id}` | `manager` | Partial `Item` | `Item` | Logs `PriceChange` rows on tier price changes for 3-day offline sync |
| POST | `/items/{id}/image` | `manager` | `{ image: dataUrl \| null }` | `Item` | Decodes JPEG/PNG/WebP base64 in memory (no Base64 stored in DB); stores in `product-images` bucket / media |
| GET | `/items/{item_id}/batches` | `manager`, `accountant`, `warehouse`, `staff` | — | `Batch[]` | Warehouse scope enforced for `warehouse`/`staff`; `purchase_price` hidden for `staff`/`rep` |
| GET | `/batches` | `manager`, `accountant`, `warehouse`, `staff` | `?warehouse_id=` | `Batch[]` | `own_warehouse` enforced; `purchase_price` hidden for non-cost roles |
| GET | `/stock` | `manager`, `accountant`, `warehouse`, `staff` | `?warehouse_id=` | `ShelfRow[]` | Physical qty, reserved qty, available shelf qty, locked flag, near-expiry status |
| POST | `/batches/{id}/hold` | `manager`, `warehouse` | `{ reason }` | `Batch` | Syncs hold across all sibling batches sharing `(item_id, expiry_date, receipt_id)`; notifies manager + keepers |
| POST | `/batches/{id}/release` | `manager` | — | `Batch` | Releases sibling batches across warehouses (`BAD_STATE` if not held) |
| GET | `/batches/{id}/trace` | `manager`, `accountant`, `warehouse`, `staff` | — | `BatchTrace[]` | Shows customers/invoices/quantities delivered from this batch (respects warehouse scope) |
| GET | `/damaged` | `manager`, `accountant`, `warehouse`, `staff` | `?warehouse_id=` | `DamagedRecord[]` | Lists damage events from stocktakes, returns, delivery problems |

### D. Receipts (Purchasing)
| Method | Path | Auth / Roles | Input | Response (`data`) | Key Errors & Rules |
| --- | --- | --- | --- | --- | --- |
| GET | `/receipts` | `manager`, `accountant`, `warehouse`, `staff` | — | `Receipt[]` | `total_cost` & line `purchase_price` hidden for `staff`; warehouse scoped |
| GET | `/receipts/{id}` | `manager`, `accountant`, `warehouse`, `staff` | — | `Receipt` | Same role/warehouse guards |
| POST | `/receipts` | `manager`, `accountant`, `warehouse` | `NewReceipt` | `Receipt` | Atomic batch creation (`expiry_date >= today_baghdad`), updates weighted `Item.avg_cost`, logs `InventoryMovement` |

### E. Transfers & Stocktakes
| Method | Path | Auth / Roles | Input | Response (`data`) | Key Errors & Rules |
| --- | --- | --- | --- | --- | --- |
| GET | `/transfers` | `manager`, `accountant`, `warehouse`, `staff` | — | `Transfer[]` | Scoped to source or destination warehouse for `warehouse`/`staff` |
| POST | `/transfers` | `manager`, `warehouse` | `NewTransfer` (+ `Idempotency-Key`) | `Transfer` | Checks `!countLocked` in both warehouses; validates shelf stock; deducts FEFO batches & records batch allocations |
| POST | `/transfers/{id}/receive` | `manager`, `warehouse` (dest) | `{ received: Record<itemId, qty>, reason }` | `Transfer` | Creates batches in dest warehouse preserving original expiry & receipt lineage; logs discrepancy if received != sent |
| POST | `/transfers/{id}/review` | `manager` | — | `Transfer` | Marks discrepancy reviewed |
| GET | `/stocktakes` | `manager`, `accountant`, `warehouse`, `staff` | — | `Stocktake[]` | Scoped to `own_warehouse` |
| GET | `/stocktakes/{id}` | `manager`, `accountant`, `warehouse`, `staff` | — | `Stocktake` | Scoped to `own_warehouse` |
| POST | `/stocktakes` | `manager`, `warehouse`, `staff` | `{ warehouse_id, category? }` | `Stocktake` | Fails if conflicting open stocktake exists; locks affected scope (`countLocked`) |
| PATCH | `/stocktakes/{id}` | `manager`, `warehouse`, `staff` | `{ lines }` | `Stocktake` | Updates `counted_qty` + optional `damaged_qty`/`reason` while open |
| POST | `/stocktakes/{id}/close` | `manager`, `warehouse` | — | `Stocktake` | Adjusts batch quantities atomically (FEFO deduct shortage / add variance batch), records `DamagedInventory`, unlocks warehouse |

### F. Customers, Special Prices & Multi-Shop
| Method | Path | Auth / Roles | Input | Response (`data`) | Key Errors & Rules |
| --- | --- | --- | --- | --- | --- |
| GET | `/customers` | Auth (not `shop`?) | — | `Customer[]` | Scoped by role (`rep` assigned/all per visibility rules; `shop` own shop); hides `balance`/`debt_limit` from `warehouse`/`staff` |
| GET | `/customers/{id}` | Auth | — | `Customer` | Same visibility + field hiding |
| POST | `/customers` | `manager`, `accountant`, `rep` | `NewCustomer` | `Customer` | Unique normalized phone, full address card |
| PATCH | `/customers/{id}` | `manager`, `accountant` | Partial `Customer` | `Customer` | Approving `pending -> active` assigns `tier_id`/`debt_limit`/`rep_id` & notifies shop |
| POST | `/customers/{id}/reject` | `manager`, `accountant` | `{ reason }` | `Customer` | Rejects `pending` shop application with reason |
| GET | `/customers/{customer_id}/statement` | `manager`, `accountant`, `rep`, `shop` (own) | `?from=&to=` | `Statement` | Running ledger of invoices, payments, reversals, returns, credit notes, refunds |
| GET | `/customers/{customer_id}/allocation` | `manager`, `accountant`, `rep`, `shop` (own) | `?amount=` | `AllocationPreview` | Simulates FIFO invoice allocation for requested amount |
| GET | `/customers/{customer_id}/credit-notes` | `manager`, `accountant`, `rep`, `shop` (own) | — | `CreditNote[]` | Lists customer credit notes |
| GET | `/customers/{customer_id}/refunds` | `manager`, `accountant`, `rep`, `shop` (own) | — | `Refund[]` | Lists customer refunds |
| POST | `/customers/{customer_id}/refunds` | `manager`, `accountant` | `{ amount, method, payment_id?, reason }` | `Refund` | Requires overpaid/negative balance or specific refundable amount |
| GET | `/customers/{customer_id}/prices` | `manager`, `accountant`, `rep`, `shop` (own) | — | `SpecialPrice[]` | Customer special price overrides |
| POST | `/customers/{customer_id}/prices` | `manager` | `{ item_id, prices: { carton_price, piece_price } \| null }` | `SpecialPrice[]` | Upserts or deletes special price |
| GET | `/me/shops` | `shop` | — | `MyShop[]` | Lists all shops linked to logged-in phone/user |
| POST | `/me/shop` | `shop` | `{ customer_id }` | `User` | Switches active `customer_id` on user session |
| POST | `/me/shops` | `shop` | `NewShopInput` | `MyShop` | Adds an additional active branch shop for same phone owner |

### G. Orders
| Method | Path | Auth / Roles | Input | Response (`data`) | Key Errors & Rules |
| --- | --- | --- | --- | --- | --- |
| GET | `/orders` | Auth | — | `Order[]` | Strict `canSeeOrder` filtering; `delivery.pin` stripped unless owning `shop` |
| GET | `/orders/{id}` | Auth | — | `Order` | Strict visibility + PIN protection |
| POST | `/orders` | `manager`, `rep`, `shop`, `accountant` | `NewOrder` (+ `Idempotency-Key`) | `Order` | `client_ref` dedup; `rep` kind/customer/category check; `shop` <=100 qty & active status; offline 3-day `PriceChange`; partial `offline` split; 250-IQD rounding; `DEBT_LIMIT`/approval routing |
| POST | `/orders/{id}/accept` | `manager`, `warehouse` | — | `Order` | Transitions `pending -> approved` (validates stock & warehouse access) |
| GET | `/orders/{id}/prep-plan` | `manager`, `warehouse` | — | `PrepPlan` | Returns per-line stock availability across warehouses |
| POST | `/orders/{id}/prepare` | `manager`, `warehouse` | `{ warehouses: Record<lineId, warehouseId> }` | `Order` | Assigns multi-warehouse `prep_parts`, verifies `!countLocked` & stock, marks keeper part prepared; when all parts ready generates `Delivery` QR + 4-digit PIN |
| POST | `/orders/{id}/approve` | `manager`, `accountant` | — | `Order` | Approves `needs_approval` (`manager` required for `discount` > 3%, `accountant`/`manager` for `debt`); sets `approved` |
| PATCH | `/orders/{id}` | `manager`, `accountant`, `rep` (own pre-delivery), `shop` (own pending) | `OrderEdit` | `Order` | Recomputes stock reservations, pricing, rounding |
| POST | `/orders/{id}/discount` | `manager`, `accountant`, `rep` | `{ percent }` | `Order` | Non-manager > 3% routes to `needs_approval` (`discount`); max 30% |
| POST | `/orders/{id}/cancel` | `manager`, `accountant`, `shop` (own pre-prep), `rep` (own pre-prep) | `{ reason }` | `Order` | Releases reservation; sets `cancelled` |

### H. Deliveries
| Method | Path | Auth / Roles | Input | Response (`data`) | Key Errors & Rules |
| --- | --- | --- | --- | --- | --- |
| POST | `/deliveries/dispatch` | `rep` (`delivery`) | `{ code }` | `Order` | Parses QR/text code; moves `ready -> out`; assigns rep if unassigned; sends PIN notification to shop owner |
| POST | `/orders/{id}/deliver` | `rep` (`delivery`, assigned) | `DeliverInput { pin, delivered, rest?, photo }` | `{ ok: true, order } \| { ok: false, left, locked }` | Wrong PIN increments & commits `pin_attempts` (locks at 5); valid PIN saves photo to private storage, FEFO-deducts batches, creates `Invoice` + optional `rest="keep"` child order |
| POST | `/orders/{id}/delivery/unlock` | `manager` | — | `Order` | Resets `pin_attempts=0, locked=false` |
| POST | `/deliveries/problem` | `rep` (`delivery`, assigned) | `ProblemInput` | `Order[]` | Records delivery failure/problem reason & notifies manager |
| POST | `/orders/{id}/delivery/return` | `manager`, `warehouse` | — | `Order` | Confirms undelivered stock returned to warehouse |
| GET | `/orders/{id}/delivery/photo` | `canSeeOrder` | — | `string \| null` | Returns authorized short-lived signed URL (or protected backend media URL in local mode) |

### I. Invoices, Payments, Returns & Online Qi Card
| Method | Path | Auth / Roles | Input | Response (`data`) | Key Errors & Rules |
| --- | --- | --- | --- | --- | --- |
| GET | `/invoices` | Auth | — | `Invoice[]` | Role visibility + `cost`/`profit` & `paid`/`remaining` redaction |
| GET | `/invoices/{id}` | Auth | — | `Invoice` | Same visibility + redaction |
| POST | `/invoices/{invoice_id}/credit` | `manager`, `accountant` | `{ amount, reason }` | `CreditNote` | Cannot exceed `invoiceRemaining`; updates `Invoice.credited` |
| GET | `/payments` | Auth | — | `Payment[]` | `shop`/`rep` scoped; warehouse/staff restricted |
| GET | `/payments/{id}` | Auth | — | `Payment` | Same |
| POST | `/payments` | `manager`, `accountant`, `rep` | `NewPayment` (+ `Idempotency-Key`) | `Payment` | `client_ref` dedup; FIFO allocates across customer's unpaid invoices; records commission eligibility |
| POST | `/payments/online` | `shop` (or permitted) | `StartOnlinePayment` (+ `Idempotency-Key`) | `OnlinePayment` | Initiates Qi Card transaction via gateway adapter; returns `form_url` |
| GET | `/payments/online` | `manager`, `accountant`, `shop` | — | `OnlinePayment[]` | Scoped to shop or admin |
| GET | `/payments/online/{id}` | `manager`, `accountant`, `shop` | — | `OnlinePayment` | Queries status safely (polling gateway if pending & verification configured) |
| POST | `/payments/{id}/reverse` | `manager`, `accountant` | `{ reason }` | `Payment` | Checks `!ALREADY_REVERSED`, `!MONTH_CLOSED`; creates `PaymentReversal` & restores invoice balances |
| POST | `/webhooks/qi-card` | Public (HMAC/RSA signature verified) | Gateway callback JSON | `{ ok: true }` | Verifies cryptographic signature & settles `OnlinePayment` idempotently into `Payment` |
| GET | `/returns` | `manager`, `accountant`, `warehouse`, `rep`, `shop` | — | `ReturnDoc[]` | Scoped by role & warehouse |
| GET | `/returns/{id}` | Same | — | `ReturnDoc` | Same |
| POST | `/returns` | `manager`, `accountant`, `warehouse`, `rep` | `NewReturn` (+ `Idempotency-Key`) | `ReturnDoc` | Validates returnable qtys (`RETURN_TOO_MUCH`); `good` restores batch qty, `damaged` records `DamagedInventory`; reduces invoice remaining |

### J. Commissions, FX, Reports, Audit, Chat, Notifications & Infra
| Method | Path | Auth / Roles | Input / Query | Response (`data`) | Key Errors & Rules |
| --- | --- | --- | --- | --- | --- |
| GET | `/commissions` | `manager`, `accountant`, `rep` | `?month=YYYY-MM` | `CommissionRow[]` | `rep` sees only self |
| POST | `/commissions/close` | `manager` | `{ month }` | `ClosedMonth` | Locks month (`MONTH_CLOSED` for later payment reversals in month) |
| GET | `/commissions/closed` | `manager`, `accountant`, `rep` | — | `ClosedMonth[]` | Closed month snapshots |
| GET | `/commissions/lines` | `manager`, `accountant`, `rep` | `?month=YYYY-MM` | `CommissionLine[]` | Detailed invoice/payment commission breakdown |
| GET | `/usd-rates` | Auth | — | `UsdRate[]` | Historical IQD/USD rates |
| POST | `/usd-rates` | `manager`, `accountant` | `{ rate }` | `UsdRate` | Logs new rate & notifies relevant roles |
| GET | `/dashboard` | Auth (operational) | `?warehouse_id=` | `Dashboard` | Strict redaction (`month_profit`, `total_debt`, `over_limit_customers`, `today_collected`) |
| GET | `/reports/daily-sales` | `manager`, `accountant`, `rep`, `warehouse`, `staff` | `?date=&warehouse_id=` | `DailySalesReport` | Role redaction |
| GET | `/reports/sales` | `manager`, `accountant`, `rep` | `?group_by=&from=&to=&warehouse_id=` | `SalesRow[]` | `group_by in {rep,item,warehouse,customer}`; hides profit for non-profit roles |
| GET | `/reports/slow-moving` | `manager`, `accountant`, `warehouse`, `staff` | `?days=&warehouse_id=` | `SlowItemRow[]` | Unsold shelf inventory threshold |
| GET | `/reports/near-expiry` | `manager`, `accountant`, `warehouse`, `staff` | `?days=&warehouse_id=` | `NearExpiryRow[]` | Batches within window (excluding expired/held) |
| GET | `/reports/debts` | `manager`, `accountant`, `rep` | — | `DebtRow[]` | Overdue & outstanding customer debt |
| GET | `/chat/me` | `shop` | — | `ChatMessage[]` | Own active customer thread |
| POST | `/chat/me` | `shop` | `{ text }` | `ChatMessage` | Posts support message from shop |
| GET | `/chat` | `manager`, `accountant`, `rep` | — | `ChatThread[]` | Lists customer support conversations + unread count |
| GET | `/chat/{customer_id}` | `manager`, `accountant`, `rep` | — | `ChatMessage[]` | Thread messages |
| POST | `/chat/{customer_id}` | `manager`, `accountant`, `rep` | `{ text }` | `ChatMessage` | Sends staff reply |
| POST | `/chat/{customer_id}/read` | `manager`, `accountant`, `rep` | — | `{ ok: true }` | Updates `ChatReadCursor` |
| GET | `/audit` | `manager`, `accountant` | `?limit=&action=&entity=` | `AuditEntry[]` | Immutable mutation logs |
| GET | `/notifications` | Auth | — | `Notification[]` | Scoped to user / role |
| POST | `/notifications/{id}/read` | Auth | — | `{ ok: true }` | Marks single read |
| POST | `/notifications/read-all` | Auth | — | `{ ok: true }` | Marks all user notifications read |
| GET | `/health/live` | Public | — | `{ status: "ok" }` | Process liveness check |
| GET | `/health/ready` | Public | — | `{ status: "ready", db: "ok" }` | Verifies DB connectivity |
| GET | `/metrics` | Staff/Admin or token-protected | — | Operational counters | Restricted telemetry |
| GET | `/api/v1/schema/` & `/docs/` | Dev/Auth | — | OpenAPI 3.0 schema & Swagger | Generated via `drf-spectacular` |

---

## Verified Contract & Behavior Notes (Phases 6–10)
- **URL Conventions**: `APPEND_SLASH = False` across `/api/v1/*` routes matching `frontend/lib/api/http.ts`. Standard endpoints also include `GET /health/live`, `GET /health/ready`, and `GET /metrics` (protected by `Role.MANAGER` or `Authorization: Bearer $METRICS_TOKEN`).
- **Audit Log (`GET /api/v1/audit`)**: Authorized strictly to `manager|accountant`; supports `?action=&entity=&limit=`; normalizes internal compound actions (e.g. `item.price_change`) to frontend enum (`create|update|cancel|reverse|transfer|price_change|approve|confirm|close|login`) and returns both bilingual `summary` and `summary_en`.
- **Notifications (`GET /api/v1/notifications`, `POST /api/v1/notifications/{id}/read`, `POST /api/v1/notifications/read-all`)**: Per-user read status via `NotificationRead` even for role broadcasts; derives frontend route links (`order` -> `/orders/{id}`, `invoice` -> `/invoices/{id}`, `payment` -> `/payments/{id}`, `return` -> `/returns/{id}`, `customer` -> `/customers/{id}`, `item` -> `/items/{id}`, `batch` -> `/batches`, `commission` -> `/commissions`, `settings` -> `/settings`, `chat` -> `/support`).
- **Support Chat**:
  - `GET /api/v1/chat/me` (`shop`) -> `{ messages: ChatMessage[], unread: int }`
  - `POST /api/v1/chat/me` (`shop`, `{ text }`) -> `201 ChatMessage`
  - `GET /api/v1/chat` (`manager|accountant|rep`) -> `[{ customer_id, customer_name, area, phone, last_message, last_at, unread }]` (`rep` scoped to assigned customers)
  - `GET|POST /api/v1/chat/{customer_id}` (`manager|accountant|rep`) -> thread messages / staff reply
  - `POST /api/v1/chat/{customer_id}/read` (`shop` own active shop | `manager|accountant|rep`) -> `{ ok: true }`
- **Invoices & Confidential Field Redaction**: `Invoice` responses omit `cost` and `profit` keys for roles outside `{manager, accountant}`, and omit `paid` and `remaining` for `warehouse` and `staff`.
- **Qi Card Online Payments**: `POST /api/v1/payments/online`, `GET /api/v1/payments/online/{request_id}`, and `POST /api/v1/webhooks/qi-card` (HMAC-SHA256 `X-Signature` verified). Rejects initiation cleanly (`503` / `BAD_STATE`) when `QI_CARD_MODE=disabled` or credentials are absent.
- **OpenAPI Docs**: Served at `/api/v1/schema/` and `/api/v1/docs/` when `API_ENABLE_DOCS=true`.

---

## 3. Evolution Additions: Supply Orders, Live GPS, Multi-Shop Sessions & Realtime

### Supply Orders (`/api/v1/supply-orders`)
| Method | Path | Auth / Roles | Input | Response (`data`) | Key Errors & Rules |
| --- | --- | --- | --- | --- | --- |
| GET | `/supply-orders` | `manager`, `accountant`, `warehouse` | — | `SupplyOrder[]` | `warehouse` scoped strictly to own `warehouse_id` |
| GET | `/supply-orders/{id}` | `manager`, `accountant`, `warehouse` | — | `SupplyOrder` | Cross-warehouse lookup by `warehouse` returns `404 NOT_FOUND` (IDOR protection) |
| POST | `/supply-orders` | `manager`, `accountant` | `{ warehouse_id, supplier, supplier_phone?, expected_date?, note?, lines: [{ item_id, unit, qty, purchase_price? }] }` | `201 SupplyOrder` | Initial status `sent`; `qty >= 1`; updates `received_qty` & transitions `sent -> partial -> received` when `POST /receipts` includes `supply_order_id` |
| POST | `/supply-orders/{id}/cancel` | `manager`, `accountant` | `{ reason }` | `SupplyOrder` | Rejects (`BAD_STATE`) if `received` or already `cancelled`; sets `cancelled` + audit log |

### Delivery Rep Live GPS (`/api/v1/deliveries/location*`)
| Method | Path | Auth / Roles | Input | Response (`data`) | Key Errors & Rules |
| --- | --- | --- | --- | --- | --- |
| POST | `/deliveries/location` | `rep` (`delivery` kind or active rep) | `{ lat, lng }` | `{ rep_id, lat, lng, at }` | Validates `-90 <= lat <= 90`, `-180 <= lng <= 180`; upserts `deliveries_rep_location`; fans out WS `location` event after commit to managers & shops with `status="out"` orders for this rep |
| GET | `/deliveries/locations` | `manager`, `accountant`, `warehouse`, `rep`, `shop` | — | `{ rep_id, lat, lng, at }[]` | Filters fresh locations within recent 10-minute window; `shop` sees ONLY reps currently carrying an active `"out"` delivery for its accessible shop(s) |

### Multi-Shop Session Isolation & Shop Onboarding (`POST /api/v1/auth/register`, `/me/shops`, `/me/shop`)
- `POST /api/v1/auth/login` and `POST /api/v1/auth/register` create an `UserSession` (`accounts_user_session`) with unique `session_id` embedded as a JWT claim (`sid`), isolating `active_customer_id` across concurrent devices sharing the same phone login.
- `POST /api/v1/me/shops` validates full structured address card via `clean_shop_address` (normalized keys `city`/`area`/`street`/`landmark`/`phone`/`details`) and enforces the maximum `10` active/pending branch shops per shop-owner user (`VALIDATION` on overflow).
- `POST /api/v1/me/shop` (`{ customer_id }`) atomically switches `UserSession.active_customer_id` (and updates active `ShopMembership`) after verifying membership ownership, leaving concurrent sessions on other devices untouched.
- `Warehouse` metadata in `/lookups` exposes `capacity` (`>= 1`), `lat`, and `lng` coordinates for supply capacity and dispatch views.

### WebSocket Ticket Handshake & Real-time Channel (`/api/v1/realtime/ticket`, `WS /ws`)
| Method / Protocol | Path | Auth | Behavior |
| --- | --- | --- | --- |
| POST | `/api/v1/realtime/ticket` | Authenticated JWT (`SessionAwareJWTAuthentication`) | Issues a single-use random ticket (`{ ticket, expires_in }`, SHA-256 digest stored in `realtime_ticket`, default TTL `60s` via `REALTIME_TICKET_TTL_SECONDS`, bound to `user` + `session_id` + `active_customer_id`) |
| WebSocket | `/ws?ticket=<ticket>` or `/ws?token=<jwt>` | Single-use DB ticket or JWT access token | Atomically redeems unexpired `used_at IS NULL` ticket (or validates active `SessionAwareJWTAuthentication` token matching `frontend/lib/realtime.ts`), checks `User.active`, non-revoked `UserSession`, and profile working hours, enforces `Origin` ACL (`4003`), per-user connection cap (`4008`), max frame size (`4009`), and sliding rate limits (`4029` / `RATE_LIMITED`). Supports `subscribe` / `unsubscribe` / `publish` across `notifications`, `chat`, `orders`, `location`. |

---

## 4. Enterprise Parity Additions: Site Config, Profiles, Rules, Vehicles, Shipments, Expenses & Targets

| Method | Path | Auth / Roles | Input | Response (`data`) | Key Rules |
| --- | --- | --- | --- | --- | --- |
| GET | `/site` | Public / Auth | — | `SitePayload` | Public callers receive branding only; authenticated callers receive full `profiles`, `apps`, `fields`, `rules` |
| PATCH | `/settings/site` | `manager` | Partial `SiteConfig` | `SitePayload` | Updates branding, `categories`, `profiles`, `apps`, `fields`, `rules` |
| POST | `/users/{id}/profile` | `manager` | `{ profile_id }` | `User` | Assigns or clears permission profile (`denied`, `read_only`, `deny_actions`, `hours`, `limits`) |
| DELETE | `/users/{id}` | `manager` | — | `{ ok: true }` | Soft-deletes inactive/former user cleanly & frees phone for reuse; blocks self-delete (`409`) |
| GET | `/setup/health` | `manager` | — | `SecurityHealth` | Returns score, 6 weighted checks, 24h login breakdown, idle & `weak` password users |
| GET | `/setup/rule-runs` | `manager` | — | `RuleRun[]` | Lists recent automation rule execution logs |
| POST | `/me/photo` | Auth | `{ photo: dataUrl \| null }` | `User` | Self profile photo upload/clear (allowed even under `read_only` profile) |
| POST | `/me/password` | Auth | `{ current, next }` | `{ ok: true }` | Validates current password, >= 6 chars, updates `weak_password`, bumps `token_version` |
| DELETE | `/me/shops` | `shop` | `{ customer_id, password }` | `{ ok, shops }` | Shop self-removes branch membership after password verification |
| DELETE | `/warehouses/{id}` | `manager` | — | `{ ok: true }` | Deletes unused warehouse; rejects `409` if referenced by users/stock/orders |
| PATCH | `/warehouses/{id}/layout` | `manager`, `warehouse` (own/cover) | Layout JSON | `Warehouse` | Saves floor/zone rack map |
| POST | `/warehouses/{id}/cover` | `manager` | `{ user_id, hours?, until? }` | `Warehouse` | Grants temporary warehouse cover to another user |
| PATCH | `/items/{id}/quick` | `manager` | `ItemQuick` | `Item` | Quick rename (`name`/`name_en`) & tier price adjustment with `PriceChange` log |
| POST | `/categories/rename` | `manager` | `{ from, to }` (also accepts `old`/`next`) | `{ old, next, updated_items }` | Renames item category across items & `SiteConfig` |
| POST | `/vehicles` | `manager` | `VehicleInput` | `Vehicle` | Creates delivery vehicle with `capacity_kg` and optional assigned `driver_id` |
| PATCH | `/vehicles/{id}` | `manager` | Partial `VehicleInput` | `Vehicle` | Updates vehicle details/status |
| POST | `/orders/{id}/shipments` | `manager`, `warehouse` | `ShipmentLoad[]` | `Order` | Splits prepared order across vehicles (`shipment_of`, `shipment_no`) & checks `capacity_kg` |
| POST | `/orders/{id}/delivery/load` | `manager`, `warehouse` | — | `Order` | Marks prepared order loaded on vehicle & notifies assigned driver |
| GET / POST | `/expenses` | `manager`, `accountant` (`view`/`create`) | `NewExpense` | `Expense[]` / `Expense` | Auto-approves below `expense_approval_above`; routes large expenses to `pending` |
| PATCH | `/expenses/{id}` | `manager`, `accountant` | Partial `Expense` | `Expense` | Edits pending expense |
| POST | `/expenses/{id}/approve|reject` | `manager` | `{ reason? }` | `Expense` | Approves or rejects pending expense (`month_expenses` / `month_net` reflect approved expenses) |
| GET | `/reps/targets` | `manager`, `accountant`, `rep` | `?month=YYYY-MM` | `RepTargetRow[]` | Sales rep monthly targets & realized progress (reps see self only) |

---

## 15. New Missing Endpoints & Gateway Extensions

| Verb | Path | Auth / Roles | Request / Query | Response `data` | Domain Logic & Security Rules |
| --- | --- | --- | --- | --- | --- |
| GET | `/catalog` | **Public (No Auth)** | None | `CatalogItem[]` | Active items only: `id`, `name`, `name_en`, `category`, `brand`, `image`, `pieces_per_carton`, `pack_name`, `piece_name`, `sells`, `in_stock`. Strictly hides prices, costs, and barcodes. |
| POST | `/counter-sales` | `warehouse` | `CounterSaleInput` | `Invoice` | Warehouse keeper direct counter sale. Atomically deducts FEFO stock, creates confirmed walk-in order and fully paid invoice (`cash` or `transfer`), generates payment ledger. Supports `client_ref` & `Idempotency-Key`. |
| DELETE | `/items/{id}` | `manager` | None | `{ "result": "removed" \| "hidden" }` | Fails with `409` if stock > 0, on open order, or open supply order. If never used: hard-deletes (`removed`). If referenced historically: soft-deletes (`hidden`, `deleted=True`, `active=False`). |
| POST | `/items/{id}/video` | `manager` | Multipart upload `file` or JSON `{ video }` | `Item` | Uploads short item demo video (mp4/webm/mov <= 20 MB) or sets video URL. |
| POST | `/orders/{id}/rating` | `shop` | `{ stars: 1..5, note? }` | `Order` | Shop owner rates completed delivery within 7 days. Re-submitting updates rating. 1–2 stars notifies manager; 5 stars notifies delivery rep. |
| GET | `/reports/delivery-ratings` | `manager`, `accountant` | None | `RatingsReport` | Average delivery rating, 5-star distribution array `spread`, per-rep breakdown, and latest 30 ratings. |
| GET | `/shop-lists` | `shop` | None | `ShopList[]` | Active customer saved favorite lists (max 30 lists, max 80 lines each). |
| POST | `/shop-lists` | `shop` | `{ name, lines }` | `ShopList` | Creates a new saved shop list. |
| PATCH | `/shop-lists/{id}` | `shop` | `{ name?, lines? }` | `ShopList` | Updates shop's own list. |
| DELETE | `/shop-lists/{id}` | `shop` | None | `{ "ok": true }` | Deletes shop's own list. |
| POST | `/push-subscriptions` | Authenticated | `{ endpoint, keys: { p256dh, auth } }` | `{ "ok": true }` | Registers browser Web Push subscription for notifications. |
| POST | `/push-subscriptions/delete` | Authenticated | `{ endpoint }` | `{ "ok": true }` | Removes Web Push subscription. |
| POST | `/payments/online` | `shop`, `customer` | `{ amount, provider: "zaincash" \| "qi_card", return_url? }` | `OnlinePayment` | Starts online payment. For ZainCash (>= 1,000 IQD): initializes transaction via UAT `https://pg-api-uat.zaincash.iq` with OAuth2 token and returns redirect form URL. |
| GET | `/payments/online/{id}` | Authenticated | None | `OnlinePayment` | Checks status, inquires gateway, verifies JWT return token, and marks settled with FIFO invoice allocation. |

---

## 16. Visits, Cash Custody, Stock Alerts, Clearances, Dated Prices, Reorder & Day Summary

| Verb | Path | Auth / Roles | Request / Query | Response `data` | Domain Logic & Security Rules |
| --- | --- | --- | --- | --- | --- |
| GET | `/visits` | `rep`, `manager`, `accountant`, `staff` | `?day=YYYY-MM-DD&rep_id=` | `Visit[]` | Lists daily visits (`rep` sees own only). |
| POST | `/visits` | `rep` | `{ customer_id, outcome, reason?, note?, lat?, lng? }` | `Visit` | Upserts today's visit for `(rep, customer, day)`. Computes Haversine `distance_m` and `far (> 200m)`. Auto-recorded on rep order/payment. |
| POST | `/customers/{id}/visit-days` | `manager`, `staff`, `rep` (own) | `{ days: number[] }` (0–6) | `Customer` | Sets weekly planned visit days (`0 = Sun .. 6 = Sat`). |
| GET | `/reports/visits` | `manager`, `accountant`, `staff` | `?from=&to=` | `VisitsReport` | Planned vs visited vs missed vs remaining vs extra vs far, per-rep totals & no-order reasons. |
| GET | `/custody` | `manager`, `accountant`, `rep` (self) | None | `CustodyRow[]` | Per-rep cash collected, handed over, current holding, shortages, pending handover, and `oldest_at`. |
| GET | `/custody/{repId}` | `manager`, `accountant`, `rep` (self) | None | `CustodyLog` | Rep custody summary, chronological cash-in / handover-out entries, and handover list. |
| POST | `/custody/handovers` | `rep`, `manager`, `accountant` | `{ rep_id?, amount, note? }` | `CashHandover` | Rep submits `pending` handover (`<= holding`); manager/accountant records directly as `confirmed`. |
| POST | `/custody/handovers/{id}/confirm` | `manager`, `accountant` | `{ received }` | `CashHandover` | Confirms pending handover (`0 <= received <= declared`), recording any shortage (`short = declared - received`). |
| POST | `/custody/handovers/{id}/cancel` | `rep` (own), `manager`, `accountant` | None | `CashHandover` | Cancels a `pending` handover. |
| GET | `/stock-alerts` | `shop` | None | `number[]` | Item IDs active shop is waiting for. |
| POST | `/stock-alerts` | `shop` | `{ item_id, on }` | `number[]` | Subscribes or unsubscribes active shop from back-in-stock alerts. |
| GET | `/stock-alerts/waitlist` | `manager`, `accountant`, `warehouse` | None | `StockWait[]` | Items shops are waiting for, sorted by shop count desc. Auto-notifies & clears on receipt/transfer-in. |
| GET | `/clearances` | `manager`, `accountant`, `warehouse` | None | `Clearance[]` | Lists batch clearances (active first); syncs `sold` / `expired` status. |
| POST | `/clearances` | `manager` | `{ batch_id, percent }` (5–70) | `Clearance` | Starts near-expiry batch discount (`until = expiry_date - 1d`), exposed on `Item.clearance` and applied to order pricing. |
| POST | `/clearances/{id}/cancel` | `manager` | None | `Clearance` | Stops active clearance early. |
| GET | `/price-schedule` | `manager`, `accountant` | None | `ScheduledPrice[]` | Lists scheduled & applied dated price changes (auto-applies due schedules). |
| POST | `/price-schedule` | `manager` | `{ item_ids, start, percent?, prices?, note? }` | `ScheduledPrice` | Schedules future or immediate (`start <= today`) price changes; notifies 60-day active customers on rises immediately or drops on `start`. |
| POST | `/price-schedule/{id}/cancel` | `manager` | None | `ScheduledPrice` | Cancels a pending `scheduled` price change. |
| GET | `/items/{id}/price-history` | `manager`, `accountant` | None | `PriceHistory` | Tier price history (`from`/`until`) and pending scheduled changes with projected `next` prices. |
| GET | `/reorder/due` | `shop` | None | `ReorderDue[]` | In-stock items the shop orders periodically that are due for reorder based on historical order gaps. |
| GET | `/reorder/late-shops` | `rep` (own), `manager`, `accountant` | None | `LateShop[]` | Shops overdue for their next order relative to their historical cadence. |
| GET | `/reports/day-summary` | `manager`, `accountant` | `?date=YYYY-MM-DD` | `DaySummary` | Target day KPIs vs yesterday & last week, `best_rep`, `counter_sales`, `late` deliveries, `low` stock, `hour` (`settings.summary_hour`), with role profit redaction. |

