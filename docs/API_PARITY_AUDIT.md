# Project-2 Frontend <-> Backend API Parity Audit

> Audited source contracts: [`frontend/lib/api/http.ts`](file:///c:/Users/Administrator/Desktop/Alnahrian/frontend/lib/api/http.ts), [`frontend/lib/api/axios.ts`](file:///c:/Users/Administrator/Desktop/Alnahrian/frontend/lib/api/axios.ts), [`frontend/lib/realtime.ts`](file:///c:/Users/Administrator/Desktop/Alnahrian/frontend/lib/realtime.ts) vs [`backend/config/urls.py`](file:///c:/Users/Administrator/Desktop/Alnahrian/backend/config/urls.py) and `backend/apps/*/urls.py`.

## Audit Summary

- **Frontend `httpApi` exported methods (`http.ts`)**: `142` exported `httpApi` keys (`137` REST helper calls including `save` create/update helpers; `resetDemo` excluded as mock-only).
- **Additional frontend & system endpoints**:
  - `POST /api/v1/auth/refresh` (token refresh in `axios.ts`)
  - `POST /api/v1/realtime/ticket` + `WS /ws` (`?ticket=` and `?token=` in `lib/realtime.ts`)
  - `POST /api/v1/webhooks/qi-card` (Qi Card signed gateway callback)
  - `GET /api/v1/media/signed` (local signed private delivery photo retrieval)
  - `GET /health/live`, `GET /health/ready`, `GET /metrics`, `GET /api/v1/schema/`, `GET /api/v1/docs/` (+ dev `GET /media/<sub_path>`)
- **Total Registered Backend HTTP Method-Routes (excluding `/admin/`)**: **`146`** (plus **`WS /ws`**).
- **Missing Endpoints**: **`0`** (`100%` of frontend operations and WebSocket topics are implemented; `5/5` parity checks pass in `tests/test_api_parity.py`).

---

## Complete Parity Matrix (`107` Frontend `httpApi` Operations + Infrastructure)

| Domain | Frontend `httpApi` / Client Operation | Backend Method & Path | Status | Verification Notes |
| --- | --- | --- | --- | --- |
| Auth & Session | `login` | `POST /api/v1/auth/login` | PASS | Phone normalized; Argon2id check; throttled `10/min`; creates `UserSession` (`sid`) |
| Auth & Session | `register` | `POST /api/v1/auth/register` | PASS | Creates `shop` user + `Customer(status="pending")` + active `UserSession` |
| Auth & Session | `logout` | `POST /api/v1/auth/logout` | PASS | Revokes refresh JTI + sets `UserSession.revoked=True, revoked_at=now()` |
| Auth & Session | `axios.ts` refresh | `POST /api/v1/auth/refresh` | PASS | Rotates & blacklists refresh JWT while preserving `sid` session link |
| Auth & Session | `me` | `GET /api/v1/me` | PASS | Resolves per-session `active_customer_id` safely |
| Auth & Session | `lookups` | `GET /api/v1/lookups` | PASS | Returns `warehouses` (with `capacity`, `lat`, `lng`), `tiers`, `reps`, `categories`, `settings`, `usd_rate` |
| Users & Config | `listUsers` | `GET /api/v1/users` | PASS | Strictly `manager` (and `accountant`); never exposes password hash |
| Users & Config | `saveUser` (create/edit) | `POST /api/v1/users`, `PATCH /api/v1/users/{id}` | PASS | Enforces single active keeper per warehouse |
| Users & Config | `saveSettings` | `PATCH /api/v1/settings` | PASS | Persists global ERP parameters |
| Users & Config | `saveTier` (create/edit) | `POST /api/v1/price-tiers`, `PATCH /api/v1/price-tiers/{id}` | PASS | Creates/updates `PriceTier` |
| Catalog | `listItems`, `getItem` | `GET /api/v1/items`, `GET /api/v1/items/{id}` | PASS | Omits `avg_cost` for `rep`/`shop`/`staff`; tier-scoped pricing |
| Catalog | `saveItem` (create/edit) | `POST /api/v1/items`, `PATCH /api/v1/items/{id}` | PASS | Updates `ItemPrice` & logs 3-day `PriceChange` history |
| Catalog | `setItemImage` | `POST /api/v1/items/{id}/image` | PASS | Decodes upload in memory & stores object path (`image` omitted if unset) |
| Inventory | `itemBatches`, `listBatches`, `shelfStock` | `GET /items/{id}/batches`, `/batches`, `/stock` | PASS | Scopes `warehouse`/`staff` to `own_warehouse`; hides `purchase_price` from `staff` |
| Inventory | `holdBatch`, `releaseBatch`, `traceBatch`, `listDamaged` | `POST /batches/{id}/hold`, `.../release`, `GET .../trace`, `/damaged` | PASS | Hold/release syncs across sibling batches; trace lists delivered customer batches |
| Receipts | `listReceipts`, `getReceipt`, `createReceipt` | `GET /receipts`, `/receipts/{id}`, `POST /receipts` | PASS | Reconciles optional `supply_order_id`, updates `avg_cost`, creates FEFO batches, hides costs for `staff` |
| Supply Orders | `listSupplyOrders`, `getSupplyOrder`, `createSupplyOrder`, `cancelSupplyOrder` | `GET /supply-orders`, `/supply-orders/{id}`, `POST /supply-orders`, `.../cancel` | PASS | Warehouse keeper scoped (`404` cross-warehouse IDOR guard); transitions `sent -> partial -> received`/`cancelled` |
| Transfers | `listTransfers`, `createTransfer`, `receiveTransfer`, `reviewTransfer` | `GET|POST /transfers`, `POST /transfers/{id}/receive`, `.../review` | PASS | Checks `!countLocked`; preserves expiry/receipt lineage; flags discrepancy until manager review |
| Stocktakes | `listStocktakes`, `getStocktake`, `createStocktake`, `saveStocktake`, `closeStocktake` | `GET|POST /stocktakes`, `GET|PATCH /stocktakes/{id}`, `POST .../close` | PASS | `manager`/`warehouse` count-lock & close adjustment (excluding `staff` from create/patch/close) |
| Customers | `listCustomers`, `getCustomer`, `saveCustomer`, `rejectCustomer` | `GET|POST /customers`, `GET|PATCH /customers/{id}`, `POST .../reject` | PASS | Role scoping + financial redaction from `warehouse`/`staff` |
| Customers | `statement`, `previewAllocation`, `creditNotes`, `refunds`, `refundCustomer` | `GET /customers/{id}/{statement,allocation,credit-notes,refunds}`, `POST .../refunds` | PASS | Running ledger, FIFO preview, overpayment refund guards |
| Customers | `specialPrices`, `setSpecialPrice` | `GET|POST /customers/{id}/prices` | PASS | Customer-specific override before tier pricing |
| Multi-Shop | `myShops`, `switchShop`, `addShop` | `GET|POST /me/shops`, `POST /me/shop` | PASS | Isolated per `UserSession.active_customer_id`; `clean_shop_address`; max 10 shops limit |
| Orders | `listOrders`, `getOrder`, `createOrder`, `updateOrder` | `GET|POST /orders`, `GET|PATCH /orders/{id}` | PASS | Offline 3-day `PriceChange` + partial split into `waiting_stock`, 250-IQD rounding, idempotency |
| Orders | `acceptOrder`, `prepPlan`, `prepareOrder`, `approveOrder`, `setOrderDiscount`, `cancelOrder` | `POST .../accept`, `GET .../prep-plan`, `POST .../{prepare,approve,discount,cancel}` | PASS | Multi-warehouse prep parts, `3%` vs `30%` discount approval, reservation release on cancel |
| Deliveries | `dispatchDelivery`, `deliverOrder`, `unlockDelivery` | `POST /deliveries/dispatch`, `POST /orders/{id}/deliver`, `.../delivery/unlock` | PASS | QR `NAHRAIN:DLV:*`, persistent 5-attempt PIN lock (`pin_encrypted`+`pin_hash`), `rest="keep"` split |
| Deliveries | `reportDeliveryProblem`, `returnToWarehouse`, `getDeliveryPhoto` | `POST /deliveries/problem`, `POST /orders/{id}/delivery/return`, `GET .../photo` | PASS | Private photo signed URL; `pin` exposed ONLY to owning `shop` |
| Live GPS | `sendLocation`, `repLocations` | `POST /deliveries/location`, `GET /deliveries/locations` | PASS | Validated coords, 10-minute window, active `"out"` shop visibility + WS fanout |
| Invoices & Returns | `listInvoices`, `getInvoice`, `creditInvoice` | `GET /invoices`, `/invoices/{id}`, `POST /invoices/{id}/credit` | PASS | Role-redacted `cost`/`profit`/`paid`/`remaining`; credit <= remaining |
| Invoices & Returns | `listReturns`, `getReturn`, `createReturn` | `GET|POST /returns`, `GET /returns/{id}` | PASS | `good` restocks linked `InvoiceBatchAllocation` FIFO; `damaged` logs `DamagedInventory` |
| Payments & Online | `listPayments`, `getPayment`, `createPayment`, `reversePayment` | `GET|POST /payments`, `GET /payments/{id}`, `POST .../reverse` | PASS | FIFO invoice allocation, idempotency, closed commission month lock (`MONTH_CLOSED`) |
| Payments & Online | `startOnlinePayment`, `getOnlinePayment`, `listOnlinePayments`, Qi Webhook | `POST|GET /payments/online`, `GET /payments/online/{key}`, `POST /webhooks/qi-card` | PASS | HMAC-SHA256 webhook verification; fail-closed `BAD_STATE` when `QI_CARD_MODE=disabled` (live gateway untested without credentials) |
| Commissions & FX | `commissions`, `closeMonth`, `closedMonths`, `commissionLines`, `usdRates`, `setUsdRate` | `GET /commissions`, `POST .../close`, `GET .../closed`, `.../lines`, `GET|POST /usd-rates` | PASS | Weighted effective rate, return clawback, immutable `ClosedCommissionMonth` snapshot |
| Reports | `dashboard`, `dailySales`, `salesReport`, `slowMoving`, `nearExpiry`, `debts` | `GET /dashboard`, `GET /reports/{daily-sales,sales,slow-moving,near-expiry,debts}` | PASS | Role redaction of `profit` and `debt` KPIs |
| Chat, Notifications & Audit | `myChat`, `sendChat`, `chatThreads`, `getChat`, `replyChat`, `markChatRead`, `listNotifications`, `markRead`, `markAllRead`, `listAudit` | `GET|POST /chat/me`, `GET /chat`, `GET|POST /chat/{id}`, `POST /chat/{id}/read`, `GET /notifications`, `POST .../{id}/read`, `.../read-all`, `GET /audit` | PASS | Per-user `NotificationRead` & `ChatReadCursor`; normalized bilingual audit entries |
| Realtime & Ops | `realtime.ts` WS handshake + subscriptions; health/metrics/schema | `POST /api/v1/realtime/ticket`, `WS /ws`, `GET /health/{live,ready}`, `/metrics` | PASS | Ticket replay/expiry/origin/rate/ACL & `Bearer $METRICS_TOKEN` scrape tests pass locally via `WebsocketCommunicator`/Django test client; live Redis/Postgres unrun on host |

---

## Phase 11 Re-Verification Addendum

- `tests/test_api_parity.py` (`5/5` passing) verified every REST HTTP operation in `frontend/lib/api/http.ts` (`get`/`post`/`patch`/`put`/`del`/`save` — `137` helper calls across `142` `httpApi` keys; `resetDemo` excluded as mock-only) against Django CBV handlers (`146` route-method handlers excluding `/admin/`) with `APPEND_SLASH = False`, matching `ErrorCode` constants in `frontend/lib/api/errors.ts` and WebSocket `/realtime/ticket` + `?token=`/`?ticket=` upgrade handling in `frontend/lib/realtime.ts`.
- **Additional Enterprise Parity Endpoints Added & Verified (`tests/test_enterprise_parity_suite.py`, `6/6` passing)**:
  - `GET /api/v1/site` (public branding), `PATCH /api/v1/settings/site` (branding, profiles, custom fields, automation rules), `POST /api/v1/users/{id}/profile`, `GET /api/v1/setup/health`, `GET /api/v1/setup/rule-runs`
  - `POST /api/v1/me/photo`, `POST /api/v1/me/password`, `DELETE /api/v1/me/shops`, `DELETE /api/v1/users/{id}`
  - `POST /api/v1/warehouses`, `DELETE /api/v1/warehouses/{id}`, `PATCH /api/v1/warehouses/{id}/layout`, `POST /api/v1/warehouses/{id}/cover`
  - `PATCH /api/v1/items/{id}/quick`, `POST /api/v1/categories/rename`
  - `POST /api/v1/vehicles`, `PATCH /api/v1/vehicles/{id}`, `POST /api/v1/orders/{id}/shipments`, `POST /api/v1/orders/{id}/delivery/load`
  - `GET|POST /api/v1/expenses`, `PATCH /api/v1/expenses/{id}`, `POST /api/v1/expenses/{id}/approve|reject`, `GET /api/v1/reps/targets`
- Phase 11 remediated a defect in [`backend/apps/common/health.py`](file:///c:/Users/Administrator/Desktop/Alnahrian/backend/apps/common/health.py) (`MetricsView`): `Authorization: Bearer $METRICS_TOKEN` previously returned `401` when `JWTAuthentication` executed before `_is_authorized`; added constant-time token handling in `perform_authentication()`.
- Frontend build verification (`frontend/`): `npm ci`, `npx next typegen`, `npx tsc --noEmit`, `npm run lint`, `npm run build` (`56` routes), and `npm run build:pages` (`48` static export routes into `frontend/out/`) all exited `0`.
