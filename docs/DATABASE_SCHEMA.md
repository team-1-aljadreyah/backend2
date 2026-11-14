# Database Schema & Ledger Architecture

> **Authoritative Schema Owner**: Django 5.2 ORM + migrations (`backend/apps/*/models.py`).  
> Identical schema runs on SQLite (dev) and Supabase PostgreSQL via `psycopg` v3 (staging/production).

---

## 1. Core Architectural Patterns
1. **Whole IQD Monetary Fields**: Stored as integer fields (`BigIntegerField` or `DecimalField(max_digits=14, decimal_places=0)`) so binary float inaccuracy never occurs. Percentages & FX rates use `DecimalField`.
2. **Inventory Accounting (Batch & Movement Ledger)**:
   - Every physical piece resides in an immutable-source `Batch` row with `qty_received` (fixed) and `qty_left` (mutated only inside atomic locked transactions with `CheckConstraint(qty_left__gte=0)`).
   - Every change to stock emits an append-only `InventoryMovement` entry (`receipt`, `sale`, `transfer_out`, `transfer_in`, `return_good`, `return_damaged`, `stocktake_adjust`, `damage`).
3. **Financial Accounting (Immutable Documents + Allocation Ledger)**:
   - `Invoice` rows are never edited or deleted after creation; adjustments happen solely via `Payment`, `PaymentAllocation`, `PaymentReversal`, `CreditNote`, `ReturnDoc`, and `Refund`.
   - Commission eligibility reads directly from `PaymentAllocation` minus reversals and proportional `ReturnDoc` commission deductions, snapshotted at `ClosedCommissionMonth`.
4. **Concurrency Locking (PostgreSQL)**:
   - Critical operations acquire row locks via `select_for_update()` ordered consistently (`order_by("id")`) on `Batch`, `Order`, `Customer`, `Invoice`, and `OnlinePayment` rows inside `@transaction.atomic`.

---

## 2. Entity Inventory & Relationships

### `accounts`
- **`User`** (`AbstractBaseUser`, `PermissionsMixin`):
  - `id` (PK), `phone` (CharField, unique, normalized `07XXXXXXXXX`), `name`, `role` (`manager|accountant|warehouse|staff|rep|shop`), `active` (bool), `warehouse` (FK -> `warehouses.Warehouse`, nullable), `customer` (FK -> `customers.Customer`, nullable, active shop for `shop` role), `rep_kinds` (JSON list: `sales`, `delivery`), `rep_categories` (JSON list of category names), `created_at`.
  - DB Partial Unique Constraint (where `active=True, role='warehouse'`): max 1 active keeper per `warehouse_id`.
- **`PriceTier`**: `id`, `name`, `name_en`, `sort_order`.
- **`SystemSetting`**: Singleton config row (`id=1`): `reservation_hours`, `near_expiry_days`, `commission_pct`, `usd_rate`, `phone`, `address`.

### `warehouses`
- **`Warehouse`**: `id`, `name`, `name_en`, `code` (unique), `active`.

### `catalog`
- **`Category`**: `id`, `name`, `name_en`, `sort_order`.
- **`Item`**: `id`, `name`, `name_en`, `category` (CharField / FK), `pieces_per_carton` (`CheckConstraint >= 1`), `min_stock` (`>= 0`), `avg_cost` (IQD int), `image_path` (CharField nullable — object path in storage, NEVER Base64), `active`.
- **`ItemPrice`**: `id`, `item` (FK), `tier` (FK `PriceTier`), `carton_price` (IQD >= 0), `piece_price` (IQD >= 0). Unique `(item, tier)`.
- **`PriceChange`**: `id`, `item` (FK), `tier` (FK), `carton_price`, `piece_price`, `effective_from` (DateTimeField, indexed). Used to resolve historical offline order prices within 3 days.

### `inventory` & `receipts`
- **`Receipt`**: `id`, `warehouse` (FK), `supplier_name`, `reference`, `total_cost`, `created_by` (FK `User`), `created_at`.
- **`ReceiptLine`**: `id`, `receipt` (FK), `item` (FK), `qty_pieces`, `qty_input`, `unit`, `purchase_price` (IQD/piece), `expiry_date`.
- **`Batch`**: `id`, `item` (FK), `warehouse` (FK), `receipt` (FK nullable), `origin_batch` (FK self nullable for transfers), `qty_received`, `qty_left` (`CheckConstraint >= 0`), `purchase_price` (IQD/piece), `expiry_date` (DateField, indexed `(item, warehouse, expiry_date)`), `held_reason` (nullable), `held_by` (FK nullable), `held_at` (nullable), `received_at`.
- **`InventoryMovement`**: `id`, `warehouse` (FK), `item` (FK), `batch` (FK nullable), `kind`, `qty_delta`, `balance_after`, `ref_type`, `ref_id`, `actor` (FK nullable), `created_at`.
- **`DamagedInventory`**: `id`, `warehouse` (FK), `item` (FK), `batch` (FK nullable), `qty`, `source` (`stocktake|return|delivery_problem`), `ref_id`, `reason`, `recorded_by` (FK), `created_at`.

### `transfers` & `stocktakes`
- **`Transfer`**: `id`, `from_warehouse` (FK), `to_warehouse` (FK), `status` (`sent|received|discrepancy`), `reason`, `client_ref` (unique nullable), `created_by`, `received_by` (nullable), `reviewed_by` (nullable), `created_at`, `received_at`, `reviewed_at`.
- **`TransferLine`**: `id`, `transfer` (FK), `item` (FK), `sent_qty`, `received_qty` (nullable).
- **`TransferBatchAllocation`**: `id`, `transfer_line` (FK), `source_batch` (FK), `qty`, `expiry_date`, `purchase_price`, `receipt_id`. Ensures received stock retains original expiry & receipt trace.
- **`Stocktake`**: `id`, `warehouse` (FK), `category` (nullable), `status` (`open|closed`), `created_by`, `closed_by` (nullable), `created_at`, `closed_at`.
  - Unique/exclusion guard preventing overlapping open stocktakes in the same warehouse/category scope.
- **`StocktakeLine`**: `id`, `stocktake` (FK), `item` (FK), `system_qty`, `counted_qty` (nullable), `damaged_qty`, `reason`.

### `customers`
- **`Customer`**: `id`, `name`, `phone` (normalized), `city`, `area`, `street`, `landmark`, `lat`, `lng`, `tier` (FK `PriceTier`), `rep` (FK `User` nullable), `debt_limit` (IQD >= 0), `status` (`active|pending|rejected`), `reject_reason`, `approved_by`, `created_at`.
- **`ShopMembership`**: `id`, `user` (FK `User`), `customer` (FK `Customer`), `is_primary`, `created_at`. Unique `(user, customer)`.
- **`SpecialPrice`**: `id`, `customer` (FK), `item` (FK), `carton_price`, `piece_price`, `updated_by`, `updated_at`. Unique `(customer, item)`.
- **`CreditNote`**: `id`, `customer` (FK), `invoice` (FK), `amount` (`> 0`), `reason`, `created_by`, `created_at`.
- **`Refund`**: `id`, `customer` (FK), `payment` (FK nullable), `amount` (`> 0`), `method` (`cash|transfer|online`), `reason`, `created_by`, `created_at`.

### `orders` & `deliveries`
- **`Order`**: `id`, `customer` (FK), `warehouse` (FK), `rep` (FK nullable), `created_by` (FK), `status` (`pending|needs_approval|approved|ready|out|delivered|cancelled|expired`), `waiting_stock` (bool), `approval_for` (JSON list), `subtotal`, `discount_percent`, `discount_amount`, `rounding`, `total`, `reserved_until` (nullable), `client_ref` (unique nullable), `offline` (bool), `recorded_at` (nullable), `parent` (FK self nullable), `rest_order` (FK self nullable), `cancel_reason`, `cancelled_at`, `created_at`.
- **`OrderLine`**: `id`, `order` (FK), `item` (FK), `warehouse` (FK nullable — set during multi-warehouse prep), `unit` (`carton|piece`), `qty`, `pieces`, `unit_price`, `total`.
- **`PrepPart`**: `id`, `order` (FK), `warehouse` (FK), `prepared_by` (FK nullable), `prepared_at` (nullable). Unique `(order, warehouse)`.
- **`Delivery`**: `id`, `order` (OneToOne), `code` (unique 8-char QR payload), `pin_hash` / encrypted or `shop_pin` field (server-verified; exposed ONLY to owning `shop` user — wait: `dispatchDelivery` & `orderView` display 4-digit PIN directly to the shop user, so stored safely server-side, hashed or encrypted + strictly redacted from every role except owning `shop`), `pin_attempts` (SmallInteger >= 0), `locked` (bool), `rep` (FK `User` nullable), `status` (`ready|out|delivered|problem|returned`), `photo_path` (nullable, private storage object path), `has_photo` (bool), `problem_reason`, `dispatched_at`, `delivered_at`.

### `invoices` & `payments`
- **`Invoice`**: `id`, `order` (OneToOne), `customer` (FK), `rep` (FK nullable), `warehouse` (FK), `subtotal`, `discount_amount`, `rounding`, `total`, `cost`, `profit`, `paid`, `returned`, `credited`, `usd_rate`, `delivered_at`.
- **`InvoiceLine`**: `id`, `invoice` (FK), `item` (FK), `warehouse` (FK), `unit`, `qty`, `pieces`, `unit_price`, `cost_per_piece`, `total`.
- **`InvoiceBatchAllocation`**: `id`, `invoice` (FK), `invoice_line` (FK), `batch` (FK), `customer` (FK), `qty_pieces`, `returned_pieces`, `created_at`. Powers `traceBatch` and exact batch restoration on `good` returns!
- **`Payment`**: `id`, `customer` (FK), `collected_by` (FK `User` nullable), `amount` (`> 0`), `method` (`cash|transfer|online`), `notes`, `client_ref` (unique nullable), `reversed` (bool), `reversed_at`, `reversed_by`, `reverse_reason`, `created_at`.
- **`PaymentAllocation`**: `id`, `payment` (FK), `invoice` (FK), `amount` (`> 0`), `created_at`.
- **`OnlinePayment`**: `id`, `customer` (FK), `amount` (`> 0`), `status` (`created|paid|failed|expired`), `provider` (`qi_card`), `external_id` (unique nullable), `form_url`, `return_url`, `client_ref` (unique nullable), `payment` (OneToOne `Payment` nullable), `raw_payload` (JSON), `created_at`, `updated_at`.
- **`ReturnDoc`**: `id`, `invoice` (FK), `customer` (FK), `rep` (FK nullable), `warehouse` (FK), `total`, `commission_base`, `reason`, `client_ref` (unique nullable), `created_by`, `created_at`.
- **`ReturnLine`**: `id`, `return_doc` (FK), `item` (FK), `batch` (FK nullable), `condition` (`good|damaged`), `qty_pieces`, `unit_price`, `total`.

### `commissions`, `exchange_rates`, `notifications`, `chat`, `audit`, `common`
- **`UsdRate`**: `id`, `rate` (Decimal), `created_by`, `created_at`.
- **`ClosedCommissionMonth`**: `month` (CharField `YYYY-MM` PK/unique), `closed_by`, `closed_at`, `snapshot` (JSON totals per rep).
- **`Notification`**: `id`, `user` (FK nullable), `target_role` (nullable), `kind`, `title_ar`, `title_en`, `body_ar`, `body_en`, `ref_type`, `ref_id`, `read` (bool — or `NotificationRead` for role broadcasts!), `created_at`.
- **`OutboxEvent`**: `id`, `topic`, `payload` (JSON), `status` (`pending|processing|sent|failed`), `attempts`, `next_retry_at`, `last_error`, `locked_at`, `locked_by`, `created_at`, `processed_at`.
- **`ChatMessage`**: `id`, `customer` (FK), `sender` (FK `User`), `sender_role`, `text`, `created_at`.
- **`ChatReadCursor`**: `id`, `user` (FK), `customer` (FK), `last_read_at`. Unique `(user, customer)`.
- **`AuditLog`**: `id`, `actor` (FK nullable), `actor_name`, `actor_role`, `action`, `entity_type`, `entity_id`, `before` (JSON nullable), `after` (JSON nullable), `ip_address`, `created_at`.
- **`IdempotencyKey`**: `id`, `key` (CharField), `scope` (endpoint + user), `request_hash`, `response_status`, `response_body` (JSON), `created_at`. Unique `(scope, key)`.

---

## Final Verified Phase 6–10 Schema Extensions & Constraints

- **`Deliveries` (`deliveries_delivery`)**: Extended with `problem_reason`, `problem_note`, `problem_by` (FK User nullable), `problem_at` (DateTime nullable), `pin_encrypted` (Fernet-encrypted 4-digit PIN), `pin_attempts` (PositiveSmallInteger), `locked` (Boolean), `photo_path` (object storage key).
- **`Invoices` (`invoices_invoice`, `invoices_line`, `invoices_batch_allocation`, `invoices_credit_note`, `invoices_return`, `invoices_return_line`)**:
  - `Invoice`: `number` (unique), `order` (OneToOne PROTECT), `customer`, `rep`, `delivery_rep`, `warehouse`, `subtotal`, `discount_percent`, `discount`, `rounding`, `total >= 0`, `cost`, `paid >= 0`, `returned >= 0`, `credited >= 0`, `usd_rate`, `delivered_at`.
  - `InvoiceBatchAllocation`: traces exact `qty_pieces` deducted per FEFO `Batch` and `returned_pieces` with CheckConstraint `returned_pieces <= qty_pieces`.
  - `CreditNote`: `number`, `invoice`, `customer`, `amount > 0`, `reason`, `created_by`.
  - `ReturnDoc` / `ReturnLine`: tracks `amount >= 0`, `cost_recovered`, `commission_base >= 0`, `condition` (`good` restocks linked FEFO batch; `damaged` records `DamagedInventory`), unique `client_ref`.
- **`Payments` (`payments_payment`, `payments_allocation`, `payments_refund`, `payments_online`)**:
  - `Payment`: `number` (unique), `customer`, `received_by`, `method` (`cash|transfer|online`), non-zero `amount` (positive or negative counter-reversal), `unallocated_amount`, `reversed`, `reversal_of` (OneToOne self), `gateway` (JSON), unique `client_ref`.
  - `PaymentAllocation`: links `Payment` to `Invoice` FIFO with exact allocated `amount`.
  - `Refund`: `number`, `customer`, `payment` (nullable FK), `amount > 0`, `method`, `reason`, `created_by`.
  - `OnlinePayment`: `request_id` (unique), `payment_id` (unique), `customer`, `amount > 0`, `status` (`created|success|failed|cancelled|expired`), `form_url`, `return_url`, `client_ref` (unique), `recorded_payment` (OneToOne), `raw_payload`.
- **`Exchange Rates` (`exchange_rates_usd`)**: `UsdRate(date unique, rate > 0, by, at)`.
- **`Commissions` (`commissions_closed_month`)**: `ClosedCommissionMonth(month "YYYY-MM" unique, pct, snapshot JSON, total_collected, total_commission, closed_by, closed_at)`.
- **`Notifications & Outbox` (`notifications_notification`, `notifications_read`, `notifications_outbox`)**: Includes per-user `NotificationRead(notification, user)` unique constraint and durable leased `OutboxEvent(topic, payload, status, attempts, next_retry_at, last_error, locked_at, locked_by, processed_at)`.
- **`Audit Log` (`audit_log`)**: Includes bilingual `summary` and `summary_ar`, `actor_name`, `actor_role`, `action`, `entity_type`, `entity_id`, `before`/`after` JSON, `ip_address`. Append-only.
- **`Support Chat` (`chat_message`, `chat_read_cursor`)**: `ChatMessage(customer, sender, sender_kind "shop"|"staff", text, created_at)` indexed on `(customer, id)`, and `ChatReadCursor(user, customer, last_read_message, updated_at)` with unique `(user, customer)`.

---

## Unified PostgreSQL Schema & Evolution Tables (`backend/database/schema.sql`)

`backend/database/schema.sql` is the **sole `.sql` file in the repository** (enforced by `backend/tests/test_schema_sync.py`, alongside column-for-column parity against Django `apps.get_models()` and live SQLite introspection).

- **`Warehouses` (`warehouses_warehouse`)**: Added `capacity` (`>= 1`), `lat` (`NUMERIC(9,6)` in `[29.0, 38.0]`), and `lng` (`NUMERIC(9,6)` in `[38.0, 49.0]`).
- **`Multi-Shop Sessions & Membership` (`accounts_user_session`, `customers_shop_membership`)**:
  - `accounts_user_session`: `session_id` (unique), `user`, `active_customer` (FK nullable), `refresh_jti`, `ip_address`, `user_agent`, `revoked`, `revoked_at`, `created_at`, `last_seen_at`.
  - `customers_shop_membership`: `(user, customer)` unique constraint plus PostgreSQL partial unique index `uniq_user_active_shop ON (user_id) WHERE is_active_shop = TRUE`.
- **`Supply Orders` (`supply_orders_order`, `supply_orders_line`)**:
  - `supply_orders_order`: `number` (unique), `warehouse`, `supplier`, `supplier_phone`, `total >= 0`, `status` (`sent|partial|received|cancelled`), `expected_date`, `note`, `cancel_reason`, `created_by`, `cancelled_by`, `cancelled_at`, `created_at`, `updated_at`.
  - `supply_orders_line`: `supply_order`, `item`, `unit` (`carton|piece`), `qty >= 1`, `received_qty >= 0`, `purchase_price >= 0`. Linked from `receipts_receipt.supply_order_id`.
- **`Delivery Live GPS` (`deliveries_rep_location`)**: `rep` (OneToOne `accounts_user`), `lat`, `lng`, `updated_at` (indexed descending on `updated_at`).
- **`WebSocket Handshake Tickets` (`realtime_ticket`)**: `token_hash` (SHA-256 hex, unique), `user`, `session_id`, `active_customer_id`, `expires_at`, `used_at`, `ip_address`, `created_at`.
- **`PostgreSQL Views, Triggers & RLS` (`backend/database/schema.sql`)**:
  - **58 tables** aligned 1:1 with Django ORM models (`test_schema_sync.py`), **4 analytical views** (`v_item_stock_summary`, `v_invoice_summary`, `v_customer_balances`, `v_near_expiry_batches`), and **6 PL/pgSQL functions** (`erp_set_updated_at()`, `erp_prevent_immutable_mutation()`, `erp_session_user_id()`, `erp_session_role()`, `erp_session_warehouse_id()`, `erp_session_customer_id()`).
  - **17 triggers**: `updated_at` maintenance triggers (`erp_set_updated_at()`) on mutable tables (including `accounts_site_config`, `deliveries_vehicle`, `expenses_expense`, etc.) and **4 append-only / no-delete immutability triggers** (`erp_prevent_immutable_mutation()`) on `inventory_movement`, `invoices_credit_note`, `payments_payment`, and `audit_log`.
  - **43 RLS-enabled tables & policies** reading PostgreSQL session GUCs (`app.user_id`, `app.role`, `app.warehouse_id`, `app.active_customer_id`) bound via [`backend/apps/common/rls.py`](file:///c:/Users/Administrator/Desktop/Alnahrian/backend/apps/common/rls.py) for defense-in-depth when non-owner roles connect.

---

## Enterprise Parity Schema Extensions (`SiteConfig`, `Profiles`, `Vehicles`, `Expenses`)
- **`Accounts & Governance`**:
  - `accounts_user` additions: `deleted`, `deleted_at`, `deleted_phone`, `photo_path`, `profile_id`, `sales_target`, `weak_password`, `token_version` (paired with partial unique phone index excluding `deleted = TRUE`).
  - `accounts_otp_challenge`: Wevlix OTP challenge tracking (`request_id`, `phone`, `purpose`, `wevlix_message_id`, `real_sent`, `attempts`, `expires_at`, `resend_at`, `otp_token`, `token_expires_at`, `used_at`).
  - `accounts_site_config` (branding, `categories`, `profiles`, `apps`, `fields`, `rules`), `accounts_login_attempt` (24h security health telemetry), `accounts_custom_field_value`, `accounts_rule_run`.
- **`Warehouses & Catalog`**:
  - `warehouses_warehouse`: `layout` JSONB, `cover` JSONB (`{ user_id, until }` temporary warehouse coverage).
  - `catalog_item`: `weight_kg` (`CheckConstraint > 0` or nullable fallback to `guess_weight_kg`).
- **`Vehicles & Shipment Splitting`**:
  - `deliveries_vehicle` (`name`, `capacity_kg`, `driver`, `active`), `deliveries_delivery` additions (`vehicle`, `loaded_at`, `loaded_by`), and `orders_order` additions (`shipment_of`, `shipment_no`, `rule_note`, `carrier_pick`, `custom`).
- **`Expenses` (`expenses_expense`)**:
  - `Expense` (`number`, `category`, `title`, `amount > 0`, `date`, `notes`, `warehouse`, `created_by`, `status` `approved|pending|rejected`, `approved_by`, `approved_at`, `rejected_reason`).

---

## Advanced Business Rules Schema Extensions (`settings.advanced`)

- **`SystemSetting` (`accounts_system_setting`)**: Added `advanced` (`JSONField`, default `{}`) storing only rules differing from `DEFAULT_RULES` (`changed_rules()`).
- **`SettingsChange` (`accounts_settings_change`)**: Audit/history trail tracking manager rule edits (`key`, `old_val`, `new_val`, `user` FK, `user_name`, `created_at`) with RLS policy in [`backend/database/schema.sql`](file:///c:/Users/Administrator/Desktop/Alnahrian/backend/database/schema.sql).
- **`Delivery` (`deliveries_delivery`)**: Added `payment` (`JSONField`, nullable) storing door collection details (`method`, `amount`, `status`, `payment_id`, `online_id`, `ref`) recorded on `/orders/{id}/deliver`.
