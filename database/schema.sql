-- =============================================================================
-- Project-2 (Al-Nahrain Wholesale ERP) — Unified Idempotent PostgreSQL Schema
-- Target Engine: PostgreSQL 15+ / Supabase PostgreSQL
-- Authoritative File Rule: This is the ONLY .sql file in the repository.
-- Matches Django 5.2 ORM models across backend/apps/* 1:1.
-- =============================================================================

BEGIN;

-- -----------------------------------------------------------------------------
-- 0. Extensions & Utility Trigger Functions
-- -----------------------------------------------------------------------------
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- Automatically keep updated_at synchronized on mutable tables
CREATE OR REPLACE FUNCTION erp_set_updated_at()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$;

-- Enforce append-only immutability on financial & audit ledgers
CREATE OR REPLACE FUNCTION erp_prevent_immutable_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'Rows in append-only table % cannot be modified or deleted (operation: %)',
        TG_TABLE_NAME, TG_OP
        USING ERRCODE = 'check_violation';
END;
$$;

-- Supabase/Postgres RLS session helper functions (defense-in-depth when
-- app.user_id / app.role session GUCs are set via SET LOCAL)
CREATE OR REPLACE FUNCTION erp_session_user_id()
RETURNS BIGINT
LANGUAGE sql
STABLE
AS $$
    SELECT NULLIF(current_setting('app.user_id', true), '')::BIGINT;
$$;

CREATE OR REPLACE FUNCTION erp_session_role()
RETURNS TEXT
LANGUAGE sql
STABLE
AS $$
    SELECT COALESCE(NULLIF(current_setting('app.role', true), ''), 'anonymous');
$$;

CREATE OR REPLACE FUNCTION erp_session_warehouse_id()
RETURNS BIGINT
LANGUAGE sql
STABLE
AS $$
    SELECT NULLIF(current_setting('app.warehouse_id', true), '')::BIGINT;
$$;

CREATE OR REPLACE FUNCTION erp_session_customer_id()
RETURNS BIGINT
LANGUAGE sql
STABLE
AS $$
    SELECT NULLIF(current_setting('app.active_customer_id', true), '')::BIGINT;
$$;

-- -----------------------------------------------------------------------------
-- 1. Warehouses
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS warehouses_warehouse (
    id          BIGSERIAL PRIMARY KEY,
    name        VARCHAR(120) NOT NULL,
    name_en     VARCHAR(120) NOT NULL,
    code        VARCHAR(32) NOT NULL UNIQUE,
    capacity    INTEGER NOT NULL DEFAULT 1000 CHECK (capacity >= 1),
    lat         NUMERIC(9, 6) NULL CHECK (lat IS NULL OR (lat BETWEEN 29.0 AND 38.0)),
    lng         NUMERIC(9, 6) NULL CHECK (lng IS NULL OR (lng BETWEEN 38.0 AND 49.0)),
    active      BOOLEAN NOT NULL DEFAULT TRUE,
    layout      JSONB NULL,
    cover       JSONB NULL
);

COMMENT ON TABLE warehouses_warehouse IS 'Physical distribution warehouses and map coordinates/capacities.';

-- -----------------------------------------------------------------------------
-- 2. Accounts, Auth Sessions, Price Tiers & System Settings
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS accounts_user (
    id              BIGSERIAL PRIMARY KEY,
    password        VARCHAR(128) NOT NULL,
    last_login      TIMESTAMPTZ NULL,
    is_superuser    BOOLEAN NOT NULL DEFAULT FALSE,
    name            VARCHAR(150) NOT NULL,
    phone           VARCHAR(20) NOT NULL UNIQUE CHECK (deleted = TRUE OR phone ~ '^07[0-9]{9}$'),
    role            VARCHAR(20) NOT NULL CHECK (role IN ('manager', 'accountant', 'warehouse', 'staff', 'rep', 'shop')),
    active          BOOLEAN NOT NULL DEFAULT TRUE,
    deleted         BOOLEAN NOT NULL DEFAULT FALSE,
    warehouse_id    BIGINT NULL REFERENCES warehouses_warehouse(id) ON DELETE SET NULL,
    rep_kinds       JSONB NOT NULL DEFAULT '[]'::jsonb,
    rep_categories  JSONB NOT NULL DEFAULT '[]'::jsonb,
    photo_path      VARCHAR(255) NOT NULL DEFAULT '',
    profile_id      VARCHAR(64) NULL,
    sales_target    BIGINT NULL CHECK (sales_target IS NULL OR sales_target >= 0),
    weak_password   BOOLEAN NOT NULL DEFAULT FALSE,
    token_version   INTEGER NOT NULL DEFAULT 1 CHECK (token_version >= 1),
    is_staff        BOOLEAN NOT NULL DEFAULT FALSE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE accounts_user IS 'Phone-authenticated ERP users (Argon2id password hash). Max 1 active keeper per warehouse.';

CREATE INDEX IF NOT EXISTS accounts_user_role_active_idx ON accounts_user (role, active);
CREATE UNIQUE INDEX IF NOT EXISTS uniq_active_warehouse_keeper_per_warehouse
    ON accounts_user (warehouse_id)
    WHERE role = 'warehouse' AND active = TRUE AND deleted = FALSE AND warehouse_id IS NOT NULL;

DROP TRIGGER IF EXISTS trg_accounts_user_updated_at ON accounts_user;
CREATE TRIGGER trg_accounts_user_updated_at
    BEFORE UPDATE ON accounts_user
    FOR EACH ROW EXECUTE FUNCTION erp_set_updated_at();

CREATE TABLE IF NOT EXISTS accounts_price_tier (
    id          BIGSERIAL PRIMARY KEY,
    name        VARCHAR(80) NOT NULL UNIQUE,
    name_en     VARCHAR(80) NOT NULL,
    sort_order  INTEGER NOT NULL DEFAULT 0 CHECK (sort_order >= 0)
);

CREATE TABLE IF NOT EXISTS accounts_system_setting (
    id                      SMALLINT PRIMARY KEY CHECK (id = 1),
    reservation_hours       INTEGER NOT NULL DEFAULT 24 CHECK (reservation_hours BETWEEN 1 AND 720),
    near_expiry_days        INTEGER NOT NULL DEFAULT 30 CHECK (near_expiry_days BETWEEN 1 AND 365),
    slow_moving_days        INTEGER NOT NULL DEFAULT 30 CHECK (slow_moving_days BETWEEN 1 AND 365),
    auto_confirm_rep_orders BOOLEAN NOT NULL DEFAULT FALSE,
    commission_pct          NUMERIC(5, 2) NOT NULL DEFAULT 2.00 CHECK (commission_pct BETWEEN 0 AND 50),
    usd_rate                NUMERIC(12, 2) NOT NULL DEFAULT 1500.00 CHECK (usd_rate > 0),
    phone                   VARCHAR(40) NOT NULL DEFAULT '07700000000',
    address                 VARCHAR(255) NOT NULL DEFAULT 'Baghdad, Iraq',
    expense_approval_above  BIGINT NULL CHECK (expense_approval_above IS NULL OR expense_approval_above >= 0),
    summary_hour            SMALLINT NOT NULL DEFAULT 21,
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

DROP TRIGGER IF EXISTS trg_accounts_system_setting_updated_at ON accounts_system_setting;
CREATE TRIGGER trg_accounts_system_setting_updated_at
    BEFORE UPDATE ON accounts_system_setting
    FOR EACH ROW EXECUTE FUNCTION erp_set_updated_at();

CREATE TABLE IF NOT EXISTS accounts_revoked_token (
    id          BIGSERIAL PRIMARY KEY,
    jti         VARCHAR(128) NOT NULL UNIQUE,
    user_id     BIGINT NOT NULL REFERENCES accounts_user(id) ON DELETE CASCADE,
    expires_at  TIMESTAMPTZ NOT NULL,
    revoked_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS accounts_revoked_token_exp_idx ON accounts_revoked_token (expires_at);

CREATE TABLE IF NOT EXISTS accounts_site_config (
    id              SMALLINT PRIMARY KEY CHECK (id = 1),
    brand_name      VARCHAR(150) NOT NULL DEFAULT '',
    brand_name_en   VARCHAR(150) NOT NULL DEFAULT '',
    brand_tagline   VARCHAR(255) NOT NULL DEFAULT '',
    brand_tagline_en VARCHAR(255) NOT NULL DEFAULT '',
    brand_logo_path VARCHAR(255) NOT NULL DEFAULT '',
    brand_color     VARCHAR(32) NOT NULL DEFAULT '',
    brand_extra     JSONB NOT NULL DEFAULT '{}'::jsonb,
    nav             JSONB NOT NULL DEFAULT '{}'::jsonb,
    dashboard       JSONB NOT NULL DEFAULT '{}'::jsonb,
    categories      JSONB NOT NULL DEFAULT '[]'::jsonb,
    texts           JSONB NOT NULL DEFAULT '{}'::jsonb,
    layouts         JSONB NOT NULL DEFAULT '{}'::jsonb,
    look            JSONB NOT NULL DEFAULT '{}'::jsonb,
    profiles        JSONB NOT NULL DEFAULT '[]'::jsonb,
    apps            JSONB NOT NULL DEFAULT '[]'::jsonb,
    fields          JSONB NOT NULL DEFAULT '[]'::jsonb,
    rules           JSONB NOT NULL DEFAULT '[]'::jsonb,
    banners         JSONB NOT NULL DEFAULT '[]'::jsonb,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

DROP TRIGGER IF EXISTS trg_accounts_site_config_updated_at ON accounts_site_config;
CREATE TRIGGER trg_accounts_site_config_updated_at
    BEFORE UPDATE ON accounts_site_config
    FOR EACH ROW EXECUTE FUNCTION erp_set_updated_at();

CREATE TABLE IF NOT EXISTS accounts_login_attempt (
    id          BIGSERIAL PRIMARY KEY,
    phone       VARCHAR(40) NOT NULL,
    user_id     BIGINT NULL REFERENCES accounts_user(id) ON DELETE SET NULL,
    ok          BOOLEAN NOT NULL DEFAULT FALSE,
    reason      VARCHAR(40) NOT NULL DEFAULT '',
    ip_address  INET NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS acc_login_att_created_ok_idx ON accounts_login_attempt (created_at DESC, ok);
CREATE INDEX IF NOT EXISTS acc_login_att_phone_idx ON accounts_login_attempt (phone);

CREATE TABLE IF NOT EXISTS accounts_custom_field_value (
    id          BIGSERIAL PRIMARY KEY,
    object_type VARCHAR(24) NOT NULL CHECK (object_type IN ('customer', 'item', 'order')),
    object_id   BIGINT NOT NULL CHECK (object_id >= 1),
    values      JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uniq_custom_field_object UNIQUE (object_type, object_id)
);

DROP TRIGGER IF EXISTS trg_accounts_custom_field_value_updated_at ON accounts_custom_field_value;
CREATE TRIGGER trg_accounts_custom_field_value_updated_at
    BEFORE UPDATE ON accounts_custom_field_value
    FOR EACH ROW EXECUTE FUNCTION erp_set_updated_at();

CREATE TABLE IF NOT EXISTS accounts_rule_run (
    id          BIGSERIAL PRIMARY KEY,
    rule_id     VARCHAR(64) NOT NULL,
    rule_name   VARCHAR(160) NOT NULL,
    trigger     VARCHAR(40) NOT NULL,
    record      VARCHAR(120) NOT NULL,
    link        VARCHAR(255) NOT NULL DEFAULT '',
    did         VARCHAR(255) NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS accounts_rule_run_created_idx ON accounts_rule_run (created_at DESC);

-- -----------------------------------------------------------------------------
-- 3. Customers & Shop Memberships (referenced by accounts_user_session)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS customers_customer (
    id              BIGSERIAL PRIMARY KEY,
    name            VARCHAR(180) NOT NULL,
    owner_name      VARCHAR(150) NOT NULL DEFAULT '',
    phone           VARCHAR(20) NOT NULL,
    city            VARCHAR(80) NOT NULL,
    area            VARCHAR(120) NOT NULL,
    street          VARCHAR(180) NOT NULL DEFAULT '',
    landmark        VARCHAR(180) NOT NULL DEFAULT '',
    address_text    VARCHAR(255) NOT NULL DEFAULT '',
    address_info    JSONB NULL,
    lat             NUMERIC(9, 6) NULL CHECK (lat IS NULL OR (lat BETWEEN 29.0 AND 38.0)),
    lng             NUMERIC(9, 6) NULL CHECK (lng IS NULL OR (lng BETWEEN 38.0 AND 49.0)),
    tier_id         BIGINT NOT NULL REFERENCES accounts_price_tier(id) ON DELETE RESTRICT,
    rep_id          BIGINT NULL REFERENCES accounts_user(id) ON DELETE SET NULL,
    debt_limit      BIGINT NOT NULL DEFAULT 0 CHECK (debt_limit >= 0),
    visit_days      JSONB NOT NULL DEFAULT '[]'::jsonb,
    active          BOOLEAN NOT NULL DEFAULT TRUE,
    walk_in         BOOLEAN NOT NULL DEFAULT FALSE,
    status          VARCHAR(20) NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'pending', 'rejected')),
    reject_reason   TEXT NOT NULL DEFAULT '',
    approved_by_id  BIGINT NULL REFERENCES accounts_user(id) ON DELETE SET NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS customers_customer_phone_idx ON customers_customer (phone);
CREATE INDEX IF NOT EXISTS customers_customer_status_active_idx ON customers_customer (status, active);
CREATE INDEX IF NOT EXISTS customers_customer_rep_idx ON customers_customer (rep_id);

DROP TRIGGER IF EXISTS trg_customers_customer_updated_at ON customers_customer;
CREATE TRIGGER trg_customers_customer_updated_at
    BEFORE UPDATE ON customers_customer
    FOR EACH ROW EXECUTE FUNCTION erp_set_updated_at();

CREATE TABLE IF NOT EXISTS accounts_user_session (
    id                  BIGSERIAL PRIMARY KEY,
    session_id          VARCHAR(64) NOT NULL UNIQUE,
    user_id             BIGINT NOT NULL REFERENCES accounts_user(id) ON DELETE CASCADE,
    active_customer_id  BIGINT NULL REFERENCES customers_customer(id) ON DELETE SET NULL,
    refresh_jti         VARCHAR(128) NOT NULL DEFAULT '',
    ip_address          INET NULL,
    user_agent          VARCHAR(255) NOT NULL DEFAULT '',
    revoked             BOOLEAN NOT NULL DEFAULT FALSE,
    revoked_at          TIMESTAMPTZ NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE accounts_user_session IS 'Per-login JWT session store isolating active_customer_id across concurrent shop devices.';
CREATE INDEX IF NOT EXISTS accounts_user_session_user_revoked_idx ON accounts_user_session (user_id, revoked);

CREATE TABLE IF NOT EXISTS customers_shop_membership (
    id              BIGSERIAL PRIMARY KEY,
    user_id         BIGINT NOT NULL REFERENCES accounts_user(id) ON DELETE CASCADE,
    customer_id     BIGINT NOT NULL REFERENCES customers_customer(id) ON DELETE CASCADE,
    is_primary      BOOLEAN NOT NULL DEFAULT FALSE,
    is_active_shop  BOOLEAN NOT NULL DEFAULT FALSE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uniq_shop_membership UNIQUE (user_id, customer_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS uniq_user_active_shop
    ON customers_shop_membership (user_id)
    WHERE is_active_shop = TRUE;

-- -----------------------------------------------------------------------------
-- 4. Catalog (Categories, Items, Prices, PriceChange History)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS catalog_category (
    id          BIGSERIAL PRIMARY KEY,
    name        VARCHAR(100) NOT NULL UNIQUE,
    name_en     VARCHAR(100) NOT NULL,
    sort_order  INTEGER NOT NULL DEFAULT 0 CHECK (sort_order >= 0)
);

CREATE TABLE IF NOT EXISTS catalog_item (
    id                  BIGSERIAL PRIMARY KEY,
    name                VARCHAR(180) NOT NULL,
    name_en             VARCHAR(180) NOT NULL,
    barcode             VARCHAR(64) NOT NULL DEFAULT '',
    category            VARCHAR(100) NOT NULL,
    pieces_per_carton   INTEGER NOT NULL CHECK (pieces_per_carton >= 1),
    min_stock           INTEGER NOT NULL DEFAULT 0 CHECK (min_stock >= 0),
    avg_cost            BIGINT NOT NULL DEFAULT 0 CHECK (avg_cost >= 0),
    commission_percent  NUMERIC(5, 2) NOT NULL DEFAULT 0.00 CHECK (commission_percent >= 0 AND commission_percent <= 100),
    weight_kg           NUMERIC(8, 3) NULL CHECK (weight_kg IS NULL OR (weight_kg >= 0.01 AND weight_kg <= 100.000)),
    brand               VARCHAR(40) NOT NULL DEFAULT '',
    video               VARCHAR(500) NOT NULL DEFAULT '',
    pack_name           VARCHAR(20) NOT NULL DEFAULT '',
    piece_name          VARCHAR(20) NOT NULL DEFAULT '',
    sells               VARCHAR(10) NOT NULL DEFAULT 'both',
    offer               JSONB NULL,
    image_path          VARCHAR(500) NOT NULL DEFAULT '',
    active              BOOLEAN NOT NULL DEFAULT TRUE,
    deleted             BOOLEAN NOT NULL DEFAULT FALSE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS catalog_item_category_active_idx ON catalog_item (category, active);

DROP TRIGGER IF EXISTS trg_catalog_item_updated_at ON catalog_item;
CREATE TRIGGER trg_catalog_item_updated_at
    BEFORE UPDATE ON catalog_item
    FOR EACH ROW EXECUTE FUNCTION erp_set_updated_at();

CREATE TABLE IF NOT EXISTS catalog_item_price (
    id              BIGSERIAL PRIMARY KEY,
    item_id         BIGINT NOT NULL REFERENCES catalog_item(id) ON DELETE CASCADE,
    tier_id         BIGINT NOT NULL REFERENCES accounts_price_tier(id) ON DELETE CASCADE,
    carton_price    BIGINT NOT NULL CHECK (carton_price >= 0),
    piece_price     BIGINT NOT NULL CHECK (piece_price >= 0),
    CONSTRAINT uniq_item_price_per_tier UNIQUE (item_id, tier_id)
);

CREATE TABLE IF NOT EXISTS catalog_price_change (
    id              BIGSERIAL PRIMARY KEY,
    item_id         BIGINT NOT NULL REFERENCES catalog_item(id) ON DELETE CASCADE,
    tier_id         BIGINT NOT NULL REFERENCES accounts_price_tier(id) ON DELETE CASCADE,
    carton_price    BIGINT NOT NULL CHECK (carton_price >= 0),
    piece_price     BIGINT NOT NULL CHECK (piece_price >= 0),
    changed_by_id   BIGINT NULL REFERENCES accounts_user(id) ON DELETE SET NULL,
    effective_from  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS catalog_price_change_lookup_idx ON catalog_price_change (item_id, tier_id, effective_from DESC);

CREATE TABLE IF NOT EXISTS customers_special_price (
    id              BIGSERIAL PRIMARY KEY,
    customer_id     BIGINT NOT NULL REFERENCES customers_customer(id) ON DELETE CASCADE,
    item_id         BIGINT NOT NULL REFERENCES catalog_item(id) ON DELETE CASCADE,
    carton_price    BIGINT NOT NULL CHECK (carton_price >= 0),
    piece_price     BIGINT NOT NULL CHECK (piece_price >= 0),
    updated_by_id   BIGINT NULL REFERENCES accounts_user(id) ON DELETE SET NULL,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uniq_customer_item_special_price UNIQUE (customer_id, item_id)
);

DROP TRIGGER IF EXISTS trg_customers_special_price_updated_at ON customers_special_price;
CREATE TRIGGER trg_customers_special_price_updated_at
    BEFORE UPDATE ON customers_special_price
    FOR EACH ROW EXECUTE FUNCTION erp_set_updated_at();

CREATE TABLE IF NOT EXISTS customers_shop_list (
    id              BIGSERIAL PRIMARY KEY,
    customer_id     BIGINT NOT NULL REFERENCES customers_customer(id) ON DELETE CASCADE,
    name            VARCHAR(40) NOT NULL,
    lines           JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS customers_shop_list_customer_idx ON customers_shop_list (customer_id);

DROP TRIGGER IF EXISTS trg_customers_shop_list_updated_at ON customers_shop_list;
CREATE TRIGGER trg_customers_shop_list_updated_at
    BEFORE UPDATE ON customers_shop_list
    FOR EACH ROW EXECUTE FUNCTION erp_set_updated_at();

-- -----------------------------------------------------------------------------
-- 5. Supply Orders & Inbound Supplier Receipts
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS supply_orders_order (
    id              BIGSERIAL PRIMARY KEY,
    number          VARCHAR(40) NOT NULL UNIQUE,
    warehouse_id    BIGINT NOT NULL REFERENCES warehouses_warehouse(id) ON DELETE RESTRICT,
    supplier        VARCHAR(200) NOT NULL,
    supplier_phone  VARCHAR(32) NOT NULL DEFAULT '',
    total           BIGINT NOT NULL DEFAULT 0 CHECK (total >= 0),
    status          VARCHAR(20) NOT NULL DEFAULT 'sent' CHECK (status IN ('sent', 'partial', 'received', 'cancelled')),
    expected_date   DATE NULL,
    note            TEXT NOT NULL DEFAULT '',
    cancel_reason   TEXT NOT NULL DEFAULT '',
    closed_short    JSONB NULL,
    created_by_id   BIGINT NOT NULL REFERENCES accounts_user(id) ON DELETE RESTRICT,
    cancelled_by_id BIGINT NULL REFERENCES accounts_user(id) ON DELETE SET NULL,
    cancelled_at    TIMESTAMPTZ NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS supply_orders_wh_status_idx ON supply_orders_order (warehouse_id, status);

DROP TRIGGER IF EXISTS trg_supply_orders_order_updated_at ON supply_orders_order;
CREATE TRIGGER trg_supply_orders_order_updated_at
    BEFORE UPDATE ON supply_orders_order
    FOR EACH ROW EXECUTE FUNCTION erp_set_updated_at();

CREATE TABLE IF NOT EXISTS supply_orders_line (
    id              BIGSERIAL PRIMARY KEY,
    supply_order_id BIGINT NOT NULL REFERENCES supply_orders_order(id) ON DELETE CASCADE,
    item_id         BIGINT NOT NULL REFERENCES catalog_item(id) ON DELETE RESTRICT,
    unit            VARCHAR(16) NOT NULL CHECK (unit IN ('carton', 'piece')),
    qty             INTEGER NOT NULL CHECK (qty >= 1),
    received_qty    NUMERIC(12, 2) NOT NULL DEFAULT 0 CHECK (received_qty >= 0),
    purchase_price  BIGINT NOT NULL DEFAULT 0 CHECK (purchase_price >= 0)
);

CREATE INDEX IF NOT EXISTS supply_orders_line_order_idx ON supply_orders_line (supply_order_id, item_id);

CREATE TABLE IF NOT EXISTS receipts_receipt (
    id              BIGSERIAL PRIMARY KEY,
    number          VARCHAR(40) NOT NULL UNIQUE,
    warehouse_id    BIGINT NOT NULL REFERENCES warehouses_warehouse(id) ON DELETE RESTRICT,
    supply_order_id BIGINT NULL REFERENCES supply_orders_order(id) ON DELETE SET NULL,
    supplier        VARCHAR(200) NOT NULL DEFAULT '',
    total_cost      BIGINT NOT NULL DEFAULT 0 CHECK (total_cost >= 0),
    client_ref      VARCHAR(120) NULL UNIQUE,
    created_by_id   BIGINT NOT NULL REFERENCES accounts_user(id) ON DELETE RESTRICT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS receipts_receipt_wh_created_idx ON receipts_receipt (warehouse_id, created_at DESC);

CREATE TABLE IF NOT EXISTS receipts_line (
    id              BIGSERIAL PRIMARY KEY,
    receipt_id      BIGINT NOT NULL REFERENCES receipts_receipt(id) ON DELETE CASCADE,
    item_id         BIGINT NOT NULL REFERENCES catalog_item(id) ON DELETE RESTRICT,
    unit            VARCHAR(16) NOT NULL CHECK (unit IN ('carton', 'piece')),
    qty             INTEGER NOT NULL CHECK (qty >= 1),
    qty_pieces      INTEGER NOT NULL CHECK (qty_pieces >= 1),
    purchase_price  BIGINT NOT NULL CHECK (purchase_price >= 0),
    expiry_date     DATE NOT NULL
);

-- -----------------------------------------------------------------------------
-- 6. Inventory Batches, Immutable Movement Ledger & Damaged Records
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS inventory_batch (
    id              BIGSERIAL PRIMARY KEY,
    item_id         BIGINT NOT NULL REFERENCES catalog_item(id) ON DELETE RESTRICT,
    warehouse_id    BIGINT NOT NULL REFERENCES warehouses_warehouse(id) ON DELETE RESTRICT,
    receipt_id      BIGINT NULL REFERENCES receipts_receipt(id) ON DELETE SET NULL,
    receipt_line_id BIGINT NULL UNIQUE REFERENCES receipts_line(id) ON DELETE SET NULL,
    origin_batch_id BIGINT NULL REFERENCES inventory_batch(id) ON DELETE SET NULL,
    qty_received    INTEGER NOT NULL CHECK (qty_received >= 0),
    qty_left        INTEGER NOT NULL CHECK (qty_left >= 0),
    purchase_price  BIGINT NOT NULL CHECK (purchase_price >= 0),
    expiry_date     DATE NOT NULL,
    held_reason     VARCHAR(255) NOT NULL DEFAULT '',
    held_by_id      BIGINT NULL REFERENCES accounts_user(id) ON DELETE SET NULL,
    held_at         TIMESTAMPTZ NULL,
    received_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE inventory_batch IS 'FEFO-ordered lot inventory per warehouse with shared origin_batch recall holds.';
CREATE INDEX IF NOT EXISTS inventory_batch_fefo_idx ON inventory_batch (item_id, warehouse_id, expiry_date, id)
    WHERE qty_left > 0 AND held_at IS NULL;
CREATE INDEX IF NOT EXISTS inventory_batch_expiry_idx ON inventory_batch (expiry_date);

CREATE TABLE IF NOT EXISTS inventory_movement (
    id              BIGSERIAL PRIMARY KEY,
    warehouse_id    BIGINT NOT NULL REFERENCES warehouses_warehouse(id) ON DELETE RESTRICT,
    item_id         BIGINT NOT NULL REFERENCES catalog_item(id) ON DELETE RESTRICT,
    batch_id        BIGINT NULL REFERENCES inventory_batch(id) ON DELETE RESTRICT,
    kind            VARCHAR(24) NOT NULL CHECK (kind IN ('receipt', 'sale', 'transfer_out', 'transfer_in', 'return_good', 'stocktake_adjust')),
    qty_delta       INTEGER NOT NULL,
    balance_after   INTEGER NOT NULL,
    ref_type        VARCHAR(40) NOT NULL DEFAULT '',
    ref_id          VARCHAR(64) NOT NULL DEFAULT '',
    actor_id        BIGINT NULL REFERENCES accounts_user(id) ON DELETE SET NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS inventory_movement_ref_idx ON inventory_movement (ref_type, ref_id);
CREATE INDEX IF NOT EXISTS inventory_movement_wh_item_idx ON inventory_movement (warehouse_id, item_id, id DESC);

DROP TRIGGER IF EXISTS trg_inventory_movement_immutable ON inventory_movement;
CREATE TRIGGER trg_inventory_movement_immutable
    BEFORE UPDATE OR DELETE ON inventory_movement
    FOR EACH ROW EXECUTE FUNCTION erp_prevent_immutable_mutation();

CREATE TABLE IF NOT EXISTS inventory_damaged (
    id              BIGSERIAL PRIMARY KEY,
    warehouse_id    BIGINT NOT NULL REFERENCES warehouses_warehouse(id) ON DELETE RESTRICT,
    item_id         BIGINT NOT NULL REFERENCES catalog_item(id) ON DELETE RESTRICT,
    batch_id        BIGINT NULL REFERENCES inventory_batch(id) ON DELETE SET NULL,
    pieces          INTEGER NOT NULL CHECK (pieces >= 1),
    source          VARCHAR(20) NOT NULL CHECK (source IN ('return', 'stocktake', 'expired', 'delivery')),
    ref             VARCHAR(80) NOT NULL,
    reason          TEXT NOT NULL DEFAULT '',
    recorded_by_id  BIGINT NULL REFERENCES accounts_user(id) ON DELETE SET NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- -----------------------------------------------------------------------------
-- 7. Inter-Warehouse Transfers & Stocktakes
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS transfers_transfer (
    id                  BIGSERIAL PRIMARY KEY,
    number              VARCHAR(40) NOT NULL UNIQUE,
    from_warehouse_id   BIGINT NOT NULL REFERENCES warehouses_warehouse(id) ON DELETE RESTRICT,
    to_warehouse_id     BIGINT NOT NULL REFERENCES warehouses_warehouse(id) ON DELETE RESTRICT,
    status              VARCHAR(20) NOT NULL DEFAULT 'sent' CHECK (status IN ('sent', 'received', 'discrepancy')),
    reason              TEXT NOT NULL DEFAULT '',
    note                TEXT NOT NULL DEFAULT '',
    diff_reason         TEXT NOT NULL DEFAULT '',
    client_ref          VARCHAR(100) NULL UNIQUE,
    created_by_id       BIGINT NOT NULL REFERENCES accounts_user(id) ON DELETE RESTRICT,
    received_by_id      BIGINT NULL REFERENCES accounts_user(id) ON DELETE SET NULL,
    reviewed_by_id      BIGINT NULL REFERENCES accounts_user(id) ON DELETE SET NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    received_at         TIMESTAMPTZ NULL,
    reviewed_at         TIMESTAMPTZ NULL,
    CONSTRAINT transfer_distinct_warehouses CHECK (from_warehouse_id <> to_warehouse_id)
);

CREATE TABLE IF NOT EXISTS transfers_line (
    id              BIGSERIAL PRIMARY KEY,
    transfer_id     BIGINT NOT NULL REFERENCES transfers_transfer(id) ON DELETE CASCADE,
    item_id         BIGINT NOT NULL REFERENCES catalog_item(id) ON DELETE RESTRICT,
    pieces          INTEGER NOT NULL CHECK (pieces >= 1),
    received_pieces INTEGER NULL CHECK (received_pieces IS NULL OR received_pieces >= 0),
    CONSTRAINT uniq_transfer_item UNIQUE (transfer_id, item_id)
);

CREATE TABLE IF NOT EXISTS transfers_batch_allocation (
    id                  BIGSERIAL PRIMARY KEY,
    transfer_line_id    BIGINT NOT NULL REFERENCES transfers_line(id) ON DELETE CASCADE,
    source_batch_id     BIGINT NOT NULL REFERENCES inventory_batch(id) ON DELETE RESTRICT,
    qty                 INTEGER NOT NULL CHECK (qty >= 1),
    expiry_date         DATE NOT NULL,
    purchase_price      BIGINT NOT NULL CHECK (purchase_price >= 0),
    receipt_id          BIGINT NULL
);

CREATE TABLE IF NOT EXISTS stocktakes_stocktake (
    id              BIGSERIAL PRIMARY KEY,
    number          VARCHAR(40) NOT NULL UNIQUE,
    warehouse_id    BIGINT NOT NULL REFERENCES warehouses_warehouse(id) ON DELETE RESTRICT,
    category        VARCHAR(100) NOT NULL DEFAULT '',
    status          VARCHAR(16) NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
    created_by_id   BIGINT NOT NULL REFERENCES accounts_user(id) ON DELETE RESTRICT,
    closed_by_id    BIGINT NULL REFERENCES accounts_user(id) ON DELETE SET NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    closed_at       TIMESTAMPTZ NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS uniq_open_stocktake_per_scope
    ON stocktakes_stocktake (warehouse_id, category)
    WHERE status = 'open';

CREATE TABLE IF NOT EXISTS stocktakes_line (
    id              BIGSERIAL PRIMARY KEY,
    stocktake_id    BIGINT NOT NULL REFERENCES stocktakes_stocktake(id) ON DELETE CASCADE,
    item_id         BIGINT NOT NULL REFERENCES catalog_item(id) ON DELETE RESTRICT,
    system_qty      INTEGER NOT NULL CHECK (system_qty >= 0),
    counted_qty     INTEGER NULL CHECK (counted_qty IS NULL OR counted_qty >= 0),
    damaged_qty     INTEGER NOT NULL DEFAULT 0 CHECK (damaged_qty >= 0),
    reason          TEXT NOT NULL DEFAULT '',
    CONSTRAINT uniq_stocktake_item UNIQUE (stocktake_id, item_id)
);

-- -----------------------------------------------------------------------------
-- 8. Orders, Multi-Warehouse Preparation & Deliveries + Live Rep Locations
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS orders_order (
    id                  BIGSERIAL PRIMARY KEY,
    number              VARCHAR(40) NOT NULL UNIQUE,
    customer_id         BIGINT NOT NULL REFERENCES customers_customer(id) ON DELETE RESTRICT,
    warehouse_id        BIGINT NOT NULL REFERENCES warehouses_warehouse(id) ON DELETE RESTRICT,
    rep_id              BIGINT NULL REFERENCES accounts_user(id) ON DELETE SET NULL,
    created_by_id       BIGINT NOT NULL REFERENCES accounts_user(id) ON DELETE RESTRICT,
    source              VARCHAR(16) NOT NULL DEFAULT 'office' CHECK (source IN ('shop', 'rep', 'office', 'counter')),
    status              VARCHAR(24) NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'needs_approval', 'approved', 'confirmed', 'cancelled', 'expired')),
    waiting_stock       BOOLEAN NOT NULL DEFAULT FALSE,
    approval_for        JSONB NOT NULL DEFAULT '[]'::jsonb,
    notes               TEXT NOT NULL DEFAULT '',
    subtotal            BIGINT NOT NULL DEFAULT 0 CHECK (subtotal >= 0),
    discount_percent    NUMERIC(5, 2) NOT NULL DEFAULT 0.00 CHECK (discount_percent >= 0 AND discount_percent <= 30),
    discount_amount     BIGINT NOT NULL DEFAULT 0 CHECK (discount_amount >= 0),
    discount_by_id      BIGINT NULL REFERENCES accounts_user(id) ON DELETE SET NULL,
    discount_at         TIMESTAMPTZ NULL,
    rounding            BIGINT NOT NULL DEFAULT 0,
    total               BIGINT NOT NULL DEFAULT 0 CHECK (total >= 0),
    reserved_until      TIMESTAMPTZ NULL,
    client_ref          VARCHAR(120) NULL UNIQUE,
    offline             BOOLEAN NOT NULL DEFAULT FALSE,
    recorded_at         TIMESTAMPTZ NULL,
    parent_id           BIGINT NULL REFERENCES orders_order(id) ON DELETE SET NULL,
    rest_order_id       BIGINT NULL REFERENCES orders_order(id) ON DELETE SET NULL,
    shipment_of_id      BIGINT NULL REFERENCES orders_order(id) ON DELETE SET NULL,
    shipment_no         INTEGER NULL CHECK (shipment_no IS NULL OR shipment_no >= 1),
    rule_note           TEXT NOT NULL DEFAULT '',
    counter             JSONB NULL,
    rating              JSONB NULL,
    short_lines         JSONB NOT NULL DEFAULT '[]'::jsonb,
    cancel_reason       TEXT NOT NULL DEFAULT '',
    cancelled_at        TIMESTAMPTZ NULL,
    accepted_by_id      BIGINT NULL REFERENCES accounts_user(id) ON DELETE SET NULL,
    accepted_at         TIMESTAMPTZ NULL,
    approved_by_id      BIGINT NULL REFERENCES accounts_user(id) ON DELETE SET NULL,
    approved_at         TIMESTAMPTZ NULL,
    edited_at           TIMESTAMPTZ NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS orders_order_status_res_idx ON orders_order (status, reserved_until);
CREATE INDEX IF NOT EXISTS orders_order_customer_created_idx ON orders_order (customer_id, created_at DESC);

DROP TRIGGER IF EXISTS trg_orders_order_updated_at ON orders_order;
CREATE TRIGGER trg_orders_order_updated_at
    BEFORE UPDATE ON orders_order
    FOR EACH ROW EXECUTE FUNCTION erp_set_updated_at();

CREATE TABLE IF NOT EXISTS orders_line (
    id              BIGSERIAL PRIMARY KEY,
    order_id        BIGINT NOT NULL REFERENCES orders_order(id) ON DELETE CASCADE,
    item_id         BIGINT NOT NULL REFERENCES catalog_item(id) ON DELETE RESTRICT,
    warehouse_id    BIGINT NULL REFERENCES warehouses_warehouse(id) ON DELETE RESTRICT,
    unit            VARCHAR(16) NOT NULL CHECK (unit IN ('carton', 'piece')),
    qty             INTEGER NOT NULL CHECK (qty >= 1),
    pieces          INTEGER NOT NULL CHECK (pieces >= 1),
    unit_price      BIGINT NOT NULL CHECK (unit_price >= 0),
    offer_off       BIGINT NOT NULL DEFAULT 0 CHECK (offer_off >= 0),
    clearance       BOOLEAN NOT NULL DEFAULT FALSE,
    total           BIGINT NOT NULL CHECK (total >= 0)
);

CREATE INDEX IF NOT EXISTS orders_line_order_idx ON orders_line (order_id, item_id);

CREATE TABLE IF NOT EXISTS orders_prep_part (
    id              BIGSERIAL PRIMARY KEY,
    order_id        BIGINT NOT NULL REFERENCES orders_order(id) ON DELETE CASCADE,
    warehouse_id    BIGINT NOT NULL REFERENCES warehouses_warehouse(id) ON DELETE RESTRICT,
    prepared_by_id  BIGINT NULL REFERENCES accounts_user(id) ON DELETE SET NULL,
    prepared_at     TIMESTAMPTZ NULL,
    CONSTRAINT uniq_order_prep_part UNIQUE (order_id, warehouse_id)
);

CREATE TABLE IF NOT EXISTS deliveries_vehicle (
    id          BIGSERIAL PRIMARY KEY,
    name        VARCHAR(150) NOT NULL,
    plate       VARCHAR(64) NOT NULL DEFAULT '',
    capacity_kg INTEGER NOT NULL DEFAULT 1000 CHECK (capacity_kg BETWEEN 50 AND 40000),
    driver_id   BIGINT NULL REFERENCES accounts_user(id) ON DELETE SET NULL,
    active      BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

DROP TRIGGER IF EXISTS trg_deliveries_vehicle_updated_at ON deliveries_vehicle;
CREATE TRIGGER trg_deliveries_vehicle_updated_at
    BEFORE UPDATE ON deliveries_vehicle
    FOR EACH ROW EXECUTE FUNCTION erp_set_updated_at();

CREATE TABLE IF NOT EXISTS deliveries_delivery (
    id              BIGSERIAL PRIMARY KEY,
    order_id        BIGINT NOT NULL UNIQUE REFERENCES orders_order(id) ON DELETE CASCADE,
    code            VARCHAR(32) NOT NULL UNIQUE,
    pin_encrypted   TEXT NOT NULL DEFAULT '',
    pin_hash        VARCHAR(128) NOT NULL DEFAULT '',
    pin_attempts    SMALLINT NOT NULL DEFAULT 0 CHECK (pin_attempts >= 0),
    locked          BOOLEAN NOT NULL DEFAULT FALSE,
    rep_id          BIGINT NULL REFERENCES accounts_user(id) ON DELETE SET NULL,
    dispatched_by_id BIGINT NULL REFERENCES accounts_user(id) ON DELETE SET NULL,
    delivered_by_id BIGINT NULL REFERENCES accounts_user(id) ON DELETE SET NULL,
    vehicle_id      BIGINT NULL REFERENCES deliveries_vehicle(id) ON DELETE SET NULL,
    loaded_by_id    BIGINT NULL REFERENCES accounts_user(id) ON DELETE SET NULL,
    loaded_at       TIMESTAMPTZ NULL,
    status          VARCHAR(20) NOT NULL DEFAULT 'ready' CHECK (status IN ('ready', 'out', 'delivered', 'problem', 'returned')),
    photo_path      VARCHAR(500) NOT NULL DEFAULT '',
    problem_reason  TEXT NOT NULL DEFAULT '',
    problem_note    TEXT NOT NULL DEFAULT '',
    problem_by_id   BIGINT NULL REFERENCES accounts_user(id) ON DELETE SET NULL,
    problem_at      TIMESTAMPTZ NULL,
    dispatched_at   TIMESTAMPTZ NULL,
    out_by_id       BIGINT NULL REFERENCES accounts_user(id) ON DELETE SET NULL,
    out_at          TIMESTAMPTZ NULL,
    out_phone       VARCHAR(20) NOT NULL DEFAULT '',
    delivered_at    TIMESTAMPTZ NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE deliveries_delivery IS 'Delivery dispatch state, Fernet-encrypted + SHA-256 PIN verification with persistent 5-attempt lock.';
CREATE INDEX IF NOT EXISTS deliveries_delivery_rep_status_idx ON deliveries_delivery (rep_id, status);

CREATE TABLE IF NOT EXISTS deliveries_rep_location (
    id          BIGSERIAL PRIMARY KEY,
    rep_id      BIGINT NOT NULL UNIQUE REFERENCES accounts_user(id) ON DELETE CASCADE,
    lat         NUMERIC(9, 6) NOT NULL CHECK (lat BETWEEN -90.0 AND 90.0),
    lng         NUMERIC(9, 6) NOT NULL CHECK (lng BETWEEN -180.0 AND 180.0),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS deliveries_rep_loc_updated_idx ON deliveries_rep_location (updated_at DESC);

DROP TRIGGER IF EXISTS trg_deliveries_rep_location_updated_at ON deliveries_rep_location;
CREATE TRIGGER trg_deliveries_rep_location_updated_at
    BEFORE UPDATE ON deliveries_rep_location
    FOR EACH ROW EXECUTE FUNCTION erp_set_updated_at();

-- -----------------------------------------------------------------------------
-- 8b. Operating Expenses
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS expenses_expense (
    id              BIGSERIAL PRIMARY KEY,
    number          VARCHAR(40) NOT NULL UNIQUE,
    date            DATE NOT NULL,
    category        VARCHAR(40) NOT NULL,
    amount          BIGINT NOT NULL CHECK (amount > 0),
    method          VARCHAR(16) NOT NULL DEFAULT 'cash' CHECK (method IN ('cash', 'transfer')),
    paid_to         VARCHAR(180) NOT NULL,
    note            TEXT NOT NULL DEFAULT '',
    warehouse_id    BIGINT NULL REFERENCES warehouses_warehouse(id) ON DELETE SET NULL,
    vehicle_id      BIGINT NULL REFERENCES deliveries_vehicle(id) ON DELETE SET NULL,
    photo_path      VARCHAR(255) NOT NULL DEFAULT '',
    status          VARCHAR(20) NOT NULL DEFAULT 'approved' CHECK (status IN ('approved', 'pending', 'rejected', 'cancelled')),
    created_by_id   BIGINT NOT NULL REFERENCES accounts_user(id) ON DELETE RESTRICT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    decided_by_id   BIGINT NULL REFERENCES accounts_user(id) ON DELETE SET NULL,
    decided_at      TIMESTAMPTZ NULL,
    reject_reason   TEXT NOT NULL DEFAULT '',
    cancelled_by_id BIGINT NULL REFERENCES accounts_user(id) ON DELETE SET NULL,
    cancelled_at    TIMESTAMPTZ NULL,
    cancel_reason   TEXT NOT NULL DEFAULT '',
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS expenses_expense_date_status_idx ON expenses_expense (date DESC, status);

DROP TRIGGER IF EXISTS trg_expenses_expense_updated_at ON expenses_expense;
CREATE TRIGGER trg_expenses_expense_updated_at
    BEFORE UPDATE ON expenses_expense
    FOR EACH ROW EXECUTE FUNCTION erp_set_updated_at();

-- -----------------------------------------------------------------------------
-- 9. Invoices, Batch Allocations, Credit Notes & Returns
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS invoices_invoice (
    id                  BIGSERIAL PRIMARY KEY,
    number              VARCHAR(40) NOT NULL UNIQUE,
    order_id            BIGINT NOT NULL UNIQUE REFERENCES orders_order(id) ON DELETE RESTRICT,
    customer_id         BIGINT NOT NULL REFERENCES customers_customer(id) ON DELETE RESTRICT,
    rep_id              BIGINT NULL REFERENCES accounts_user(id) ON DELETE SET NULL,
    delivery_rep_id     BIGINT NULL REFERENCES accounts_user(id) ON DELETE SET NULL,
    warehouse_id        BIGINT NOT NULL REFERENCES warehouses_warehouse(id) ON DELETE RESTRICT,
    subtotal            BIGINT NOT NULL DEFAULT 0 CHECK (subtotal >= 0),
    discount_percent    NUMERIC(5, 2) NOT NULL DEFAULT 0.00,
    discount            BIGINT NOT NULL DEFAULT 0 CHECK (discount >= 0),
    rounding            BIGINT NOT NULL DEFAULT 0,
    total               BIGINT NOT NULL DEFAULT 0 CHECK (total >= 0),
    cost                BIGINT NOT NULL DEFAULT 0 CHECK (cost >= 0),
    paid                BIGINT NOT NULL DEFAULT 0 CHECK (paid >= 0),
    returned            BIGINT NOT NULL DEFAULT 0 CHECK (returned >= 0),
    credited            BIGINT NOT NULL DEFAULT 0 CHECK (credited >= 0),
    usd_rate            NUMERIC(12, 2) NOT NULL DEFAULT 1500.00 CHECK (usd_rate > 0),
    counter             JSONB NULL,
    created_by_id       BIGINT NULL REFERENCES accounts_user(id) ON DELETE SET NULL,
    delivered_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE invoices_invoice IS 'Immutable financial invoices issued strictly upon verified delivery.';
CREATE INDEX IF NOT EXISTS invoices_invoice_cust_deliv_idx ON invoices_invoice (customer_id, delivered_at DESC);
CREATE INDEX IF NOT EXISTS invoices_invoice_rep_deliv_idx ON invoices_invoice (rep_id, delivered_at DESC);

CREATE TABLE IF NOT EXISTS invoices_line (
    id              BIGSERIAL PRIMARY KEY,
    invoice_id      BIGINT NOT NULL REFERENCES invoices_invoice(id) ON DELETE CASCADE,
    warehouse_id    BIGINT NOT NULL REFERENCES warehouses_warehouse(id) ON DELETE RESTRICT,
    item_id         BIGINT NOT NULL REFERENCES catalog_item(id) ON DELETE RESTRICT,
    unit            VARCHAR(16) NOT NULL CHECK (unit IN ('carton', 'piece')),
    qty             INTEGER NOT NULL CHECK (qty >= 1),
    pieces          INTEGER NOT NULL CHECK (pieces >= 1),
    returned_pieces INTEGER NOT NULL DEFAULT 0 CHECK (returned_pieces >= 0 AND returned_pieces <= pieces),
    unit_price      BIGINT NOT NULL CHECK (unit_price >= 0),
    offer_off       BIGINT NOT NULL DEFAULT 0 CHECK (offer_off >= 0),
    clearance       BOOLEAN NOT NULL DEFAULT FALSE,
    total           BIGINT NOT NULL CHECK (total >= 0),
    cost            BIGINT NOT NULL DEFAULT 0 CHECK (cost >= 0)
);

CREATE TABLE IF NOT EXISTS invoices_batch_allocation (
    id              BIGSERIAL PRIMARY KEY,
    invoice_id      BIGINT NOT NULL REFERENCES invoices_invoice(id) ON DELETE CASCADE,
    invoice_line_id BIGINT NOT NULL REFERENCES invoices_line(id) ON DELETE CASCADE,
    batch_id        BIGINT NOT NULL REFERENCES inventory_batch(id) ON DELETE RESTRICT,
    customer_id     BIGINT NOT NULL REFERENCES customers_customer(id) ON DELETE RESTRICT,
    qty_pieces      INTEGER NOT NULL CHECK (qty_pieces >= 1),
    returned_pieces INTEGER NOT NULL DEFAULT 0 CHECK (returned_pieces >= 0),
    purchase_price  BIGINT NOT NULL DEFAULT 0 CHECK (purchase_price >= 0),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT alloc_returned_lte_qty CHECK (returned_pieces <= qty_pieces)
);

CREATE INDEX IF NOT EXISTS invoices_batch_alloc_batch_idx ON invoices_batch_allocation (batch_id);

CREATE TABLE IF NOT EXISTS invoices_credit_note (
    id              BIGSERIAL PRIMARY KEY,
    number          VARCHAR(40) NOT NULL UNIQUE,
    invoice_id      BIGINT NOT NULL REFERENCES invoices_invoice(id) ON DELETE RESTRICT,
    customer_id     BIGINT NOT NULL REFERENCES customers_customer(id) ON DELETE RESTRICT,
    amount          BIGINT NOT NULL CHECK (amount > 0),
    reason          TEXT NOT NULL,
    created_by_id   BIGINT NOT NULL REFERENCES accounts_user(id) ON DELETE RESTRICT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

DROP TRIGGER IF EXISTS trg_invoices_credit_note_immutable ON invoices_credit_note;
CREATE TRIGGER trg_invoices_credit_note_immutable
    BEFORE UPDATE OR DELETE ON invoices_credit_note
    FOR EACH ROW EXECUTE FUNCTION erp_prevent_immutable_mutation();

CREATE TABLE IF NOT EXISTS invoices_return (
    id              BIGSERIAL PRIMARY KEY,
    number          VARCHAR(40) NOT NULL UNIQUE,
    invoice_id      BIGINT NOT NULL REFERENCES invoices_invoice(id) ON DELETE RESTRICT,
    customer_id     BIGINT NOT NULL REFERENCES customers_customer(id) ON DELETE RESTRICT,
    rep_id          BIGINT NULL REFERENCES accounts_user(id) ON DELETE SET NULL,
    warehouse_id    BIGINT NOT NULL REFERENCES warehouses_warehouse(id) ON DELETE RESTRICT,
    amount          BIGINT NOT NULL DEFAULT 0 CHECK (amount >= 0),
    cost_recovered  BIGINT NOT NULL DEFAULT 0 CHECK (cost_recovered >= 0),
    commission_base BIGINT NOT NULL DEFAULT 0 CHECK (commission_base >= 0),
    note            TEXT NOT NULL DEFAULT '',
    client_ref      VARCHAR(120) NULL UNIQUE,
    created_by_id   BIGINT NOT NULL REFERENCES accounts_user(id) ON DELETE RESTRICT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS invoices_return_line (
    id              BIGSERIAL PRIMARY KEY,
    return_doc_id   BIGINT NOT NULL REFERENCES invoices_return(id) ON DELETE CASCADE,
    invoice_line_id BIGINT NULL REFERENCES invoices_line(id) ON DELETE SET NULL,
    item_id         BIGINT NOT NULL REFERENCES catalog_item(id) ON DELETE RESTRICT,
    batch_id        BIGINT NULL REFERENCES inventory_batch(id) ON DELETE SET NULL,
    condition       VARCHAR(16) NOT NULL DEFAULT 'good' CHECK (condition IN ('good', 'damaged')),
    unit            VARCHAR(16) NOT NULL DEFAULT 'piece' CHECK (unit IN ('carton', 'piece')),
    qty             INTEGER NOT NULL CHECK (qty >= 1),
    pieces          INTEGER NOT NULL CHECK (pieces >= 1),
    unit_price      BIGINT NOT NULL DEFAULT 0 CHECK (unit_price >= 0),
    amount          BIGINT NOT NULL DEFAULT 0 CHECK (amount >= 0),
    cost            BIGINT NOT NULL DEFAULT 0 CHECK (cost >= 0)
);

-- -----------------------------------------------------------------------------
-- 10. Payments, FIFO Allocations, Refunds, Qi Card Online & Webhook Ledger
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS payments_payment (
    id                  BIGSERIAL PRIMARY KEY,
    number              VARCHAR(40) NOT NULL UNIQUE,
    customer_id         BIGINT NOT NULL REFERENCES customers_customer(id) ON DELETE RESTRICT,
    received_by_id      BIGINT NULL REFERENCES accounts_user(id) ON DELETE SET NULL,
    received_by_name    VARCHAR(150) NOT NULL DEFAULT '',
    received_by_role    VARCHAR(24) NOT NULL DEFAULT 'shop',
    method              VARCHAR(16) NOT NULL DEFAULT 'cash' CHECK (method IN ('cash', 'transfer', 'online')),
    amount              BIGINT NOT NULL CHECK (amount <> 0),
    unallocated_amount  BIGINT NOT NULL DEFAULT 0,
    note                TEXT NOT NULL DEFAULT '',
    reason              TEXT NOT NULL DEFAULT '',
    reversed            BOOLEAN NOT NULL DEFAULT FALSE,
    reversal_of_id      BIGINT NULL UNIQUE REFERENCES payments_payment(id) ON DELETE RESTRICT,
    reversed_by_id      BIGINT NULL REFERENCES accounts_user(id) ON DELETE SET NULL,
    reversed_at         TIMESTAMPTZ NULL,
    gateway             JSONB NULL,
    client_ref          VARCHAR(120) NULL UNIQUE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS payments_payment_cust_created_idx ON payments_payment (customer_id, created_at DESC);

DROP TRIGGER IF EXISTS trg_payments_payment_no_delete ON payments_payment;
CREATE TRIGGER trg_payments_payment_no_delete
    BEFORE DELETE ON payments_payment
    FOR EACH ROW EXECUTE FUNCTION erp_prevent_immutable_mutation();

CREATE TABLE IF NOT EXISTS payments_allocation (
    id          BIGSERIAL PRIMARY KEY,
    payment_id  BIGINT NOT NULL REFERENCES payments_payment(id) ON DELETE CASCADE,
    invoice_id  BIGINT NOT NULL REFERENCES invoices_invoice(id) ON DELETE RESTRICT,
    amount      BIGINT NOT NULL CHECK (amount <> 0),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS payments_allocation_invoice_idx ON payments_allocation (invoice_id, payment_id);

CREATE TABLE IF NOT EXISTS payments_refund (
    id              BIGSERIAL PRIMARY KEY,
    number          VARCHAR(40) NOT NULL UNIQUE,
    customer_id     BIGINT NOT NULL REFERENCES customers_customer(id) ON DELETE RESTRICT,
    payment_id      BIGINT NULL REFERENCES payments_payment(id) ON DELETE SET NULL,
    amount          BIGINT NOT NULL CHECK (amount > 0),
    method          VARCHAR(16) NOT NULL DEFAULT 'cash' CHECK (method IN ('cash', 'transfer', 'online')),
    reason          TEXT NOT NULL,
    created_by_id   BIGINT NOT NULL REFERENCES accounts_user(id) ON DELETE RESTRICT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS payments_online (
    id                  BIGSERIAL PRIMARY KEY,
    request_id          VARCHAR(64) NOT NULL UNIQUE,
    payment_id          VARCHAR(120) NOT NULL UNIQUE,
    provider            VARCHAR(20) NOT NULL DEFAULT 'qi',
    gateway_id          VARCHAR(120) NOT NULL DEFAULT '',
    customer_id         BIGINT NOT NULL REFERENCES customers_customer(id) ON DELETE RESTRICT,
    initiated_by_id     BIGINT NULL REFERENCES accounts_user(id) ON DELETE SET NULL,
    amount              BIGINT NOT NULL CHECK (amount > 0),
    status              VARCHAR(20) NOT NULL DEFAULT 'created' CHECK (status IN ('created', 'success', 'failed', 'cancelled', 'expired')),
    form_url            TEXT NOT NULL DEFAULT '',
    return_url          TEXT NOT NULL DEFAULT '',
    note                VARCHAR(255) NOT NULL DEFAULT '',
    client_ref          VARCHAR(120) NULL UNIQUE,
    recorded_payment_id BIGINT NULL UNIQUE REFERENCES payments_payment(id) ON DELETE SET NULL,
    raw_payload         JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

DROP TRIGGER IF EXISTS trg_payments_online_updated_at ON payments_online;
CREATE TRIGGER trg_payments_online_updated_at
    BEFORE UPDATE ON payments_online
    FOR EACH ROW EXECUTE FUNCTION erp_set_updated_at();

CREATE TABLE IF NOT EXISTS payments_webhook_event (
    id              BIGSERIAL PRIMARY KEY,
    provider        VARCHAR(40) NOT NULL DEFAULT 'qi_card',
    event_id        VARCHAR(120) NOT NULL,
    signature_valid BOOLEAN NOT NULL DEFAULT FALSE,
    payload         JSONB NOT NULL DEFAULT '{}'::jsonb,
    processed       BOOLEAN NOT NULL DEFAULT FALSE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uniq_payment_webhook_provider_event UNIQUE (provider, event_id)
);

-- -----------------------------------------------------------------------------
-- 11. Exchange Rates & Closed Commissions
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS exchange_rates_usd (
    id      BIGSERIAL PRIMARY KEY,
    date    DATE NOT NULL UNIQUE,
    rate    NUMERIC(12, 2) NOT NULL CHECK (rate > 0),
    by_id   BIGINT NOT NULL REFERENCES accounts_user(id) ON DELETE RESTRICT,
    at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS commissions_closed_month (
    id              BIGSERIAL PRIMARY KEY,
    month           VARCHAR(7) NOT NULL UNIQUE CHECK (month ~ '^[0-9]{4}-[0-9]{2}$'),
    pct             NUMERIC(5, 2) NOT NULL DEFAULT 2.00 CHECK (pct >= 0),
    snapshot        JSONB NOT NULL DEFAULT '[]'::jsonb,
    total_collected BIGINT NOT NULL DEFAULT 0,
    total_commission BIGINT NOT NULL DEFAULT 0,
    closed_by_id    BIGINT NOT NULL REFERENCES accounts_user(id) ON DELETE RESTRICT,
    closed_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- -----------------------------------------------------------------------------
-- 12. Notifications, Outbox, Support Chat, Audit Log, Idempotency & Realtime
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS notifications_notification (
    id          BIGSERIAL PRIMARY KEY,
    user_id     BIGINT NULL REFERENCES accounts_user(id) ON DELETE CASCADE,
    target_role VARCHAR(20) NOT NULL DEFAULT '',
    kind        VARCHAR(40) NOT NULL,
    title_ar    VARCHAR(255) NOT NULL,
    title_en    VARCHAR(255) NOT NULL,
    body_ar     TEXT NOT NULL DEFAULT '',
    body_en     TEXT NOT NULL DEFAULT '',
    ref_type    VARCHAR(40) NOT NULL DEFAULT '',
    ref_id      VARCHAR(64) NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS notifications_target_idx ON notifications_notification (user_id, target_role, created_at DESC);

CREATE TABLE IF NOT EXISTS notifications_read (
    id              BIGSERIAL PRIMARY KEY,
    notification_id BIGINT NOT NULL REFERENCES notifications_notification(id) ON DELETE CASCADE,
    user_id         BIGINT NOT NULL REFERENCES accounts_user(id) ON DELETE CASCADE,
    read_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uniq_notification_read_per_user UNIQUE (notification_id, user_id)
);

CREATE TABLE IF NOT EXISTS notifications_push_subscription (
    id              BIGSERIAL PRIMARY KEY,
    user_id         BIGINT NOT NULL REFERENCES accounts_user(id) ON DELETE CASCADE,
    endpoint        VARCHAR(500) NOT NULL,
    p256dh          VARCHAR(255) NOT NULL,
    auth            VARCHAR(255) NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uniq_push_sub_user_endpoint UNIQUE (user_id, endpoint)
);

CREATE INDEX IF NOT EXISTS notifications_push_sub_endpoint_idx ON notifications_push_subscription (endpoint);

CREATE TABLE IF NOT EXISTS notifications_outbox (
    id              BIGSERIAL PRIMARY KEY,
    topic           VARCHAR(80) NOT NULL,
    payload         JSONB NOT NULL DEFAULT '{}'::jsonb,
    status          VARCHAR(20) NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'processing', 'sent', 'failed')),
    attempts        SMALLINT NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    next_retry_at   TIMESTAMPTZ NULL,
    locked_at       TIMESTAMPTZ NULL,
    locked_by       VARCHAR(64) NOT NULL DEFAULT '',
    last_error      TEXT NOT NULL DEFAULT '',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    processed_at    TIMESTAMPTZ NULL
);

CREATE INDEX IF NOT EXISTS notifications_outbox_pending_idx ON notifications_outbox (status, id)
    WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS notif_outbox_status_retry_idx ON notifications_outbox (status, next_retry_at);

CREATE TABLE IF NOT EXISTS chat_message (
    id          BIGSERIAL PRIMARY KEY,
    customer_id BIGINT NOT NULL REFERENCES customers_customer(id) ON DELETE CASCADE,
    sender_id   BIGINT NOT NULL REFERENCES accounts_user(id) ON DELETE RESTRICT,
    sender_kind VARCHAR(10) NOT NULL CHECK (sender_kind IN ('shop', 'staff')),
    text        TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS chat_msg_customer_id_idx ON chat_message (customer_id, id);

CREATE TABLE IF NOT EXISTS chat_read_cursor (
    id                  BIGSERIAL PRIMARY KEY,
    user_id             BIGINT NOT NULL REFERENCES accounts_user(id) ON DELETE CASCADE,
    customer_id         BIGINT NOT NULL REFERENCES customers_customer(id) ON DELETE CASCADE,
    last_read_message_id BIGINT NULL REFERENCES chat_message(id) ON DELETE SET NULL,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uniq_chat_read_cursor UNIQUE (user_id, customer_id)
);

CREATE TABLE IF NOT EXISTS audit_log (
    id          BIGSERIAL PRIMARY KEY,
    actor_id    BIGINT NULL REFERENCES accounts_user(id) ON DELETE SET NULL,
    actor_name  VARCHAR(150) NOT NULL,
    actor_role  VARCHAR(24) NOT NULL,
    action      VARCHAR(80) NOT NULL,
    entity_type VARCHAR(40) NOT NULL DEFAULT '',
    entity_id   VARCHAR(64) NOT NULL DEFAULT '',
    summary     VARCHAR(300) NOT NULL,
    summary_ar  VARCHAR(300) NOT NULL DEFAULT '',
    before      JSONB NULL,
    after       JSONB NULL,
    ip_address  INET NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE audit_log IS 'Append-only structured audit trail. Protected against UPDATE/DELETE by trigger.';
CREATE INDEX IF NOT EXISTS audit_log_action_created_idx ON audit_log (action, created_at DESC);
CREATE INDEX IF NOT EXISTS audit_log_entity_idx ON audit_log (entity_type, entity_id);

DROP TRIGGER IF EXISTS trg_audit_log_immutable ON audit_log;
CREATE TRIGGER trg_audit_log_immutable
    BEFORE UPDATE OR DELETE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION erp_prevent_immutable_mutation();

CREATE TABLE IF NOT EXISTS common_idempotency_key (
    id              BIGSERIAL PRIMARY KEY,
    scope           VARCHAR(120) NOT NULL,
    key             VARCHAR(128) NOT NULL,
    request_hash    VARCHAR(64) NOT NULL,
    response_status SMALLINT NOT NULL DEFAULT 200,
    response_body   JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uniq_idempotency_scope_key UNIQUE (scope, key)
);

CREATE TABLE IF NOT EXISTS common_storage_object (
    id              BIGSERIAL PRIMARY KEY,
    bucket          VARCHAR(64) NOT NULL CHECK (bucket IN ('product-images', 'delivery-proofs')),
    object_path     VARCHAR(500) NOT NULL UNIQUE,
    mime_type       VARCHAR(80) NOT NULL DEFAULT 'application/octet-stream',
    byte_size       BIGINT NOT NULL DEFAULT 0 CHECK (byte_size >= 0),
    uploaded_by_id  BIGINT NULL REFERENCES accounts_user(id) ON DELETE SET NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS common_demo_seed_record (
    id              BIGSERIAL PRIMARY KEY,
    seed_key        VARCHAR(160) NOT NULL UNIQUE,
    profile         VARCHAR(32) NOT NULL DEFAULT 'full',
    app_label       VARCHAR(64) NOT NULL,
    model_name      VARCHAR(64) NOT NULL,
    object_id       BIGINT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS common_demo_target_idx ON common_demo_seed_record (app_label, model_name, object_id);
CREATE INDEX IF NOT EXISTS common_demo_profile_idx ON common_demo_seed_record (profile);

CREATE TABLE IF NOT EXISTS realtime_ticket (
    id                  BIGSERIAL PRIMARY KEY,
    token_hash          VARCHAR(64) NOT NULL UNIQUE,
    user_id             BIGINT NOT NULL REFERENCES accounts_user(id) ON DELETE CASCADE,
    session_id          VARCHAR(64) NOT NULL DEFAULT '',
    active_customer_id  BIGINT NULL,
    expires_at          TIMESTAMPTZ NOT NULL,
    used_at             TIMESTAMPTZ NULL,
    ip_address          INET NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE realtime_ticket IS 'Short-lived SHA-256-hashed single-use WebSocket handshake tickets.';
CREATE INDEX IF NOT EXISTS realtime_ticket_exp_idx ON realtime_ticket (expires_at);

-- -----------------------------------------------------------------------------
-- 13. Analytical PostgreSQL Reporting Views
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_item_stock_summary AS
SELECT
    w.id AS warehouse_id,
    i.id AS item_id,
    COALESCE(SUM(b.qty_left) FILTER (WHERE b.expiry_date >= CURRENT_DATE), 0) AS on_shelf_pieces,
    COALESCE(SUM(b.qty_left) FILTER (WHERE b.expiry_date >= CURRENT_DATE AND b.held_at IS NULL), 0) AS non_held_pieces,
    COALESCE(SUM(b.qty_left) FILTER (WHERE b.expiry_date < CURRENT_DATE), 0) AS expired_pieces
FROM warehouses_warehouse w
CROSS JOIN catalog_item i
LEFT JOIN inventory_batch b
    ON b.warehouse_id = w.id
   AND b.item_id = i.id
   AND b.qty_left > 0
GROUP BY w.id, i.id;

CREATE OR REPLACE VIEW v_invoice_summary AS
SELECT
    inv.id,
    inv.number,
    inv.customer_id,
    inv.rep_id,
    inv.warehouse_id,
    inv.total,
    inv.paid,
    inv.returned,
    inv.credited,
    inv.cost,
    (inv.total - inv.returned - inv.credited) - inv.cost AS profit,
    GREATEST(0, inv.total - inv.paid - inv.returned - inv.credited) AS remaining,
    inv.usd_rate,
    inv.delivered_at
FROM invoices_invoice inv;

CREATE OR REPLACE VIEW v_customer_balances AS
SELECT
    c.id AS customer_id,
    c.name,
    c.debt_limit,
    COALESCE(SUM(inv.total - inv.paid - inv.returned - inv.credited), 0) AS invoice_balance,
    COALESCE((
        SELECT SUM(r.amount)
        FROM payments_refund r
        WHERE r.customer_id = c.id
    ), 0) AS total_refunds,
    GREATEST(0, COALESCE((
        SELECT SUM(p.unallocated_amount)
        FROM payments_payment p
        WHERE p.customer_id = c.id AND NOT p.reversed AND p.amount > 0
    ), 0)) AS unallocated_credit
FROM customers_customer c
LEFT JOIN invoices_invoice inv ON inv.customer_id = c.id
GROUP BY c.id, c.name, c.debt_limit;

CREATE OR REPLACE VIEW v_near_expiry_batches AS
SELECT
    b.id AS batch_id,
    b.item_id,
    b.warehouse_id,
    b.qty_left,
    b.expiry_date,
    (b.expiry_date - CURRENT_DATE) AS days_left
FROM inventory_batch b
WHERE b.qty_left > 0
  AND b.held_at IS NULL
  AND b.expiry_date >= CURRENT_DATE;

-- -----------------------------------------------------------------------------
-- 14. Supabase / PostgreSQL Row-Level Security (RLS) Defense-in-Depth Policies
-- Note: Table owners & roles with BYPASSRLS bypass RLS automatically unless
-- FORCE ROW LEVEL SECURITY is set. In production Supabase deployments, run the
-- restricted non-owner application / PostgREST role (`al_nahrain_app` /
-- `authenticated`) without BYPASSRLS, while Django's SessionAwareJWTAuthentication
-- + RequestLanguageAndAuditMiddleware bind `app.user_id`, `app.role`,
-- `app.warehouse_id`, and `app.active_customer_id` on PostgreSQL connections.
-- -----------------------------------------------------------------------------
ALTER TABLE customers_customer ENABLE ROW LEVEL SECURITY;
ALTER TABLE customers_shop_membership ENABLE ROW LEVEL SECURITY;
ALTER TABLE customers_special_price ENABLE ROW LEVEL SECURITY;
ALTER TABLE customers_shop_list ENABLE ROW LEVEL SECURITY;
ALTER TABLE accounts_user_session ENABLE ROW LEVEL SECURITY;
ALTER TABLE accounts_revoked_token ENABLE ROW LEVEL SECURITY;
ALTER TABLE accounts_site_config ENABLE ROW LEVEL SECURITY;
ALTER TABLE accounts_login_attempt ENABLE ROW LEVEL SECURITY;
ALTER TABLE accounts_custom_field_value ENABLE ROW LEVEL SECURITY;
ALTER TABLE accounts_rule_run ENABLE ROW LEVEL SECURITY;
ALTER TABLE supply_orders_order ENABLE ROW LEVEL SECURITY;
ALTER TABLE supply_orders_line ENABLE ROW LEVEL SECURITY;
ALTER TABLE receipts_receipt ENABLE ROW LEVEL SECURITY;
ALTER TABLE receipts_line ENABLE ROW LEVEL SECURITY;
ALTER TABLE inventory_batch ENABLE ROW LEVEL SECURITY;
ALTER TABLE inventory_movement ENABLE ROW LEVEL SECURITY;
ALTER TABLE inventory_damaged ENABLE ROW LEVEL SECURITY;
ALTER TABLE transfers_transfer ENABLE ROW LEVEL SECURITY;
ALTER TABLE transfers_line ENABLE ROW LEVEL SECURITY;
ALTER TABLE stocktakes_stocktake ENABLE ROW LEVEL SECURITY;
ALTER TABLE stocktakes_line ENABLE ROW LEVEL SECURITY;
ALTER TABLE orders_order ENABLE ROW LEVEL SECURITY;
ALTER TABLE orders_line ENABLE ROW LEVEL SECURITY;
ALTER TABLE orders_prep_part ENABLE ROW LEVEL SECURITY;
ALTER TABLE deliveries_delivery ENABLE ROW LEVEL SECURITY;
ALTER TABLE deliveries_vehicle ENABLE ROW LEVEL SECURITY;
ALTER TABLE deliveries_rep_location ENABLE ROW LEVEL SECURITY;
ALTER TABLE expenses_expense ENABLE ROW LEVEL SECURITY;
ALTER TABLE invoices_invoice ENABLE ROW LEVEL SECURITY;
ALTER TABLE invoices_line ENABLE ROW LEVEL SECURITY;
ALTER TABLE invoices_batch_allocation ENABLE ROW LEVEL SECURITY;
ALTER TABLE invoices_credit_note ENABLE ROW LEVEL SECURITY;
ALTER TABLE invoices_return ENABLE ROW LEVEL SECURITY;
ALTER TABLE invoices_return_line ENABLE ROW LEVEL SECURITY;
ALTER TABLE payments_payment ENABLE ROW LEVEL SECURITY;
ALTER TABLE payments_allocation ENABLE ROW LEVEL SECURITY;
ALTER TABLE payments_refund ENABLE ROW LEVEL SECURITY;
ALTER TABLE payments_online ENABLE ROW LEVEL SECURITY;
ALTER TABLE chat_message ENABLE ROW LEVEL SECURITY;
ALTER TABLE chat_read_cursor ENABLE ROW LEVEL SECURITY;
ALTER TABLE notifications_notification ENABLE ROW LEVEL SECURITY;
ALTER TABLE notifications_read ENABLE ROW LEVEL SECURITY;
ALTER TABLE notifications_push_subscription ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE realtime_ticket ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS p_customers_access ON customers_customer;
CREATE POLICY p_customers_access ON customers_customer
    FOR SELECT
    USING (
        erp_session_role() IN ('manager', 'accountant', 'warehouse', 'staff')
        OR (erp_session_role() = 'rep' AND (
            rep_id = erp_session_user_id()
            OR EXISTS (
                SELECT 1 FROM deliveries_delivery d
                JOIN orders_order o ON o.id = d.order_id
                WHERE o.customer_id = customers_customer.id
                  AND d.rep_id = erp_session_user_id()
            )
        ))
        OR (erp_session_role() = 'shop' AND (
            id = erp_session_customer_id()
            OR (erp_session_customer_id() IS NULL AND EXISTS (
                SELECT 1 FROM customers_shop_membership m
                WHERE m.customer_id = customers_customer.id AND m.user_id = erp_session_user_id()
            ))
        ))
    );

DROP POLICY IF EXISTS p_shop_membership_access ON customers_shop_membership;
CREATE POLICY p_shop_membership_access ON customers_shop_membership
    FOR ALL
    USING (
        erp_session_role() IN ('manager', 'accountant')
        OR user_id = erp_session_user_id()
    )
    WITH CHECK (
        erp_session_role() IN ('manager', 'accountant')
        OR user_id = erp_session_user_id()
    );

DROP POLICY IF EXISTS p_special_price_access ON customers_special_price;
CREATE POLICY p_special_price_access ON customers_special_price
    FOR ALL
    USING (
        erp_session_role() IN ('manager', 'accountant', 'warehouse', 'staff', 'rep')
        OR (erp_session_role() = 'shop' AND customer_id = erp_session_customer_id())
    )
    WITH CHECK (erp_session_role() = 'manager');

DROP POLICY IF EXISTS p_user_session_access ON accounts_user_session;
CREATE POLICY p_user_session_access ON accounts_user_session
    FOR ALL
    USING (user_id = erp_session_user_id() OR erp_session_role() = 'manager')
    WITH CHECK (user_id = erp_session_user_id() OR erp_session_role() = 'manager');

DROP POLICY IF EXISTS p_revoked_token_access ON accounts_revoked_token;
CREATE POLICY p_revoked_token_access ON accounts_revoked_token
    FOR ALL
    USING (user_id = erp_session_user_id() OR erp_session_role() = 'manager')
    WITH CHECK (user_id = erp_session_user_id() OR erp_session_role() = 'manager');

DROP POLICY IF EXISTS p_supply_orders_access ON supply_orders_order;
CREATE POLICY p_supply_orders_access ON supply_orders_order
    FOR ALL
    USING (
        erp_session_role() IN ('manager', 'accountant')
        OR (erp_session_role() IN ('warehouse', 'staff')
            AND (erp_session_warehouse_id() IS NULL OR warehouse_id = erp_session_warehouse_id()))
    )
    WITH CHECK (
        erp_session_role() IN ('manager', 'accountant')
        OR (erp_session_role() IN ('warehouse', 'staff')
            AND (erp_session_warehouse_id() IS NULL OR warehouse_id = erp_session_warehouse_id()))
    );

DROP POLICY IF EXISTS p_supply_order_lines_access ON supply_orders_line;
CREATE POLICY p_supply_order_lines_access ON supply_orders_line
    FOR ALL
    USING (
        EXISTS (SELECT 1 FROM supply_orders_order so WHERE so.id = supply_orders_line.supply_order_id)
    )
    WITH CHECK (
        erp_session_role() IN ('manager', 'accountant')
        OR EXISTS (SELECT 1 FROM supply_orders_order so WHERE so.id = supply_orders_line.supply_order_id)
    );

DROP POLICY IF EXISTS p_receipts_access ON receipts_receipt;
CREATE POLICY p_receipts_access ON receipts_receipt
    FOR ALL
    USING (
        erp_session_role() IN ('manager', 'accountant')
        OR (erp_session_role() IN ('warehouse', 'staff')
            AND (erp_session_warehouse_id() IS NULL OR warehouse_id = erp_session_warehouse_id()))
    )
    WITH CHECK (
        erp_session_role() IN ('manager', 'accountant', 'warehouse')
        AND (erp_session_warehouse_id() IS NULL OR warehouse_id = erp_session_warehouse_id())
    );

DROP POLICY IF EXISTS p_receipt_lines_access ON receipts_line;
CREATE POLICY p_receipt_lines_access ON receipts_line
    FOR ALL
    USING (EXISTS (SELECT 1 FROM receipts_receipt r WHERE r.id = receipts_line.receipt_id))
    WITH CHECK (EXISTS (SELECT 1 FROM receipts_receipt r WHERE r.id = receipts_line.receipt_id));

DROP POLICY IF EXISTS p_inventory_batch_access ON inventory_batch;
CREATE POLICY p_inventory_batch_access ON inventory_batch
    FOR ALL
    USING (
        erp_session_role() IN ('manager', 'accountant')
        OR (erp_session_role() IN ('warehouse', 'staff')
            AND (erp_session_warehouse_id() IS NULL OR warehouse_id = erp_session_warehouse_id()))
    )
    WITH CHECK (
        erp_session_role() IN ('manager', 'accountant', 'warehouse')
    );

DROP POLICY IF EXISTS p_inventory_movement_access ON inventory_movement;
CREATE POLICY p_inventory_movement_access ON inventory_movement
    FOR SELECT
    USING (
        erp_session_role() IN ('manager', 'accountant')
        OR (erp_session_role() IN ('warehouse', 'staff')
            AND (erp_session_warehouse_id() IS NULL OR warehouse_id = erp_session_warehouse_id()))
    );

DROP POLICY IF EXISTS p_inventory_damaged_access ON inventory_damaged;
CREATE POLICY p_inventory_damaged_access ON inventory_damaged
    FOR ALL
    USING (
        erp_session_role() IN ('manager', 'accountant')
        OR (erp_session_role() IN ('warehouse', 'staff')
            AND (erp_session_warehouse_id() IS NULL OR warehouse_id = erp_session_warehouse_id()))
    )
    WITH CHECK (erp_session_role() IN ('manager', 'warehouse'));

DROP POLICY IF EXISTS p_transfers_access ON transfers_transfer;
CREATE POLICY p_transfers_access ON transfers_transfer
    FOR ALL
    USING (
        erp_session_role() IN ('manager', 'accountant')
        OR (erp_session_role() IN ('warehouse', 'staff')
            AND (erp_session_warehouse_id() IS NULL
                OR from_warehouse_id = erp_session_warehouse_id()
                OR to_warehouse_id = erp_session_warehouse_id()))
    )
    WITH CHECK (erp_session_role() IN ('manager', 'warehouse'));

DROP POLICY IF EXISTS p_transfer_lines_access ON transfers_line;
CREATE POLICY p_transfer_lines_access ON transfers_line
    FOR ALL
    USING (EXISTS (SELECT 1 FROM transfers_transfer t WHERE t.id = transfers_line.transfer_id))
    WITH CHECK (EXISTS (SELECT 1 FROM transfers_transfer t WHERE t.id = transfers_line.transfer_id));

DROP POLICY IF EXISTS p_stocktakes_access ON stocktakes_stocktake;
CREATE POLICY p_stocktakes_access ON stocktakes_stocktake
    FOR ALL
    USING (
        erp_session_role() IN ('manager', 'accountant')
        OR (erp_session_role() IN ('warehouse', 'staff')
            AND (erp_session_warehouse_id() IS NULL OR warehouse_id = erp_session_warehouse_id()))
    )
    WITH CHECK (erp_session_role() IN ('manager', 'warehouse'));

DROP POLICY IF EXISTS p_stocktake_lines_access ON stocktakes_line;
CREATE POLICY p_stocktake_lines_access ON stocktakes_line
    FOR ALL
    USING (EXISTS (SELECT 1 FROM stocktakes_stocktake s WHERE s.id = stocktakes_line.stocktake_id))
    WITH CHECK (EXISTS (SELECT 1 FROM stocktakes_stocktake s WHERE s.id = stocktakes_line.stocktake_id));

DROP POLICY IF EXISTS p_orders_access ON orders_order;
CREATE POLICY p_orders_access ON orders_order
    FOR ALL
    USING (
        erp_session_role() IN ('manager', 'accountant')
        OR (erp_session_role() IN ('warehouse', 'staff')
            AND (erp_session_warehouse_id() IS NULL
                OR warehouse_id = erp_session_warehouse_id()
                OR EXISTS (SELECT 1 FROM orders_line ol WHERE ol.order_id = orders_order.id AND ol.warehouse_id = erp_session_warehouse_id())))
        OR (erp_session_role() = 'rep' AND (
            rep_id = erp_session_user_id()
            OR EXISTS (SELECT 1 FROM deliveries_delivery d WHERE d.order_id = orders_order.id AND d.rep_id = erp_session_user_id())
        ))
        OR (erp_session_role() = 'shop' AND customer_id = erp_session_customer_id())
    )
    WITH CHECK (
        erp_session_role() IN ('manager', 'accountant', 'warehouse', 'rep')
        OR (erp_session_role() = 'shop' AND customer_id = erp_session_customer_id())
    );

DROP POLICY IF EXISTS p_order_lines_access ON orders_line;
CREATE POLICY p_order_lines_access ON orders_line
    FOR ALL
    USING (EXISTS (SELECT 1 FROM orders_order o WHERE o.id = orders_line.order_id))
    WITH CHECK (EXISTS (SELECT 1 FROM orders_order o WHERE o.id = orders_line.order_id));

DROP POLICY IF EXISTS p_order_prep_parts_access ON orders_prep_part;
CREATE POLICY p_order_prep_parts_access ON orders_prep_part
    FOR ALL
    USING (EXISTS (SELECT 1 FROM orders_order o WHERE o.id = orders_prep_part.order_id))
    WITH CHECK (erp_session_role() IN ('manager', 'warehouse'));

DROP POLICY IF EXISTS p_deliveries_access ON deliveries_delivery;
CREATE POLICY p_deliveries_access ON deliveries_delivery
    FOR ALL
    USING (
        erp_session_role() IN ('manager', 'accountant', 'warehouse', 'staff')
        OR (erp_session_role() = 'rep' AND (
            rep_id = erp_session_user_id()
            OR EXISTS (SELECT 1 FROM orders_order o WHERE o.id = deliveries_delivery.order_id AND o.rep_id = erp_session_user_id())
        ))
        OR (erp_session_role() = 'shop' AND EXISTS (
            SELECT 1 FROM orders_order o WHERE o.id = deliveries_delivery.order_id AND o.customer_id = erp_session_customer_id()
        ))
    )
    WITH CHECK (erp_session_role() IN ('manager', 'warehouse', 'rep'));

DROP POLICY IF EXISTS p_rep_location_access ON deliveries_rep_location;
CREATE POLICY p_rep_location_access ON deliveries_rep_location
    FOR ALL
    USING (
        erp_session_role() IN ('manager', 'accountant', 'warehouse', 'staff')
        OR (erp_session_role() = 'rep' AND rep_id = erp_session_user_id())
        OR (erp_session_role() = 'shop' AND EXISTS (
            SELECT 1 FROM deliveries_delivery d
            JOIN orders_order o ON o.id = d.order_id
            WHERE d.status = 'out'
              AND o.customer_id = erp_session_customer_id()
              AND COALESCE(d.rep_id, o.rep_id) = deliveries_rep_location.rep_id
        ))
    )
    WITH CHECK (erp_session_role() = 'rep' AND rep_id = erp_session_user_id());

DROP POLICY IF EXISTS p_invoices_access ON invoices_invoice;
CREATE POLICY p_invoices_access ON invoices_invoice
    FOR SELECT
    USING (
        erp_session_role() IN ('manager', 'accountant')
        OR (erp_session_role() IN ('warehouse', 'staff')
            AND (erp_session_warehouse_id() IS NULL OR warehouse_id = erp_session_warehouse_id()))
        OR (erp_session_role() = 'rep' AND (rep_id = erp_session_user_id() OR delivery_rep_id = erp_session_user_id()))
        OR (erp_session_role() = 'shop' AND customer_id = erp_session_customer_id())
    );

DROP POLICY IF EXISTS p_invoice_lines_access ON invoices_line;
CREATE POLICY p_invoice_lines_access ON invoices_line
    FOR SELECT
    USING (EXISTS (SELECT 1 FROM invoices_invoice i WHERE i.id = invoices_line.invoice_id));

DROP POLICY IF EXISTS p_invoice_alloc_access ON invoices_batch_allocation;
CREATE POLICY p_invoice_alloc_access ON invoices_batch_allocation
    FOR SELECT
    USING (erp_session_role() IN ('manager', 'accountant', 'warehouse', 'staff'));

DROP POLICY IF EXISTS p_credit_note_access ON invoices_credit_note;
CREATE POLICY p_credit_note_access ON invoices_credit_note
    FOR SELECT
    USING (
        erp_session_role() IN ('manager', 'accountant')
        OR (erp_session_role() = 'shop' AND customer_id = erp_session_customer_id())
    );

DROP POLICY IF EXISTS p_returns_access ON invoices_return;
CREATE POLICY p_returns_access ON invoices_return
    FOR SELECT
    USING (
        erp_session_role() IN ('manager', 'accountant')
        OR (erp_session_role() IN ('warehouse', 'staff')
            AND (erp_session_warehouse_id() IS NULL OR warehouse_id = erp_session_warehouse_id()))
        OR (erp_session_role() = 'rep' AND rep_id = erp_session_user_id())
        OR (erp_session_role() = 'shop' AND customer_id = erp_session_customer_id())
    );

DROP POLICY IF EXISTS p_return_lines_access ON invoices_return_line;
CREATE POLICY p_return_lines_access ON invoices_return_line
    FOR SELECT
    USING (EXISTS (SELECT 1 FROM invoices_return r WHERE r.id = invoices_return_line.return_doc_id));

DROP POLICY IF EXISTS p_payments_access ON payments_payment;
CREATE POLICY p_payments_access ON payments_payment
    FOR SELECT
    USING (
        erp_session_role() IN ('manager', 'accountant')
        OR (erp_session_role() = 'rep' AND received_by_id = erp_session_user_id())
        OR (erp_session_role() = 'shop' AND customer_id = erp_session_customer_id())
    );

DROP POLICY IF EXISTS p_payment_alloc_access ON payments_allocation;
CREATE POLICY p_payment_alloc_access ON payments_allocation
    FOR SELECT
    USING (EXISTS (SELECT 1 FROM payments_payment p WHERE p.id = payments_allocation.payment_id));

DROP POLICY IF EXISTS p_payment_refund_access ON payments_refund;
CREATE POLICY p_payment_refund_access ON payments_refund
    FOR SELECT
    USING (
        erp_session_role() IN ('manager', 'accountant')
        OR (erp_session_role() = 'shop' AND customer_id = erp_session_customer_id())
    );

DROP POLICY IF EXISTS p_online_payment_access ON payments_online;
CREATE POLICY p_online_payment_access ON payments_online
    FOR ALL
    USING (
        erp_session_role() IN ('manager', 'accountant')
        OR (erp_session_role() = 'rep' AND initiated_by_id = erp_session_user_id())
        OR (erp_session_role() = 'shop' AND customer_id = erp_session_customer_id())
    )
    WITH CHECK (
        erp_session_role() IN ('manager', 'accountant', 'rep')
        OR (erp_session_role() = 'shop' AND customer_id = erp_session_customer_id())
    );

DROP POLICY IF EXISTS p_chat_access ON chat_message;
CREATE POLICY p_chat_access ON chat_message
    FOR ALL
    USING (
        erp_session_role() IN ('manager', 'staff')
        OR (erp_session_role() = 'shop' AND customer_id = erp_session_customer_id())
    )
    WITH CHECK (
        erp_session_role() IN ('manager', 'staff')
        OR (erp_session_role() = 'shop' AND customer_id = erp_session_customer_id())
    );

DROP POLICY IF EXISTS p_chat_cursor_access ON chat_read_cursor;
CREATE POLICY p_chat_cursor_access ON chat_read_cursor
    FOR ALL
    USING (user_id = erp_session_user_id() OR erp_session_role() IN ('manager', 'staff'))
    WITH CHECK (user_id = erp_session_user_id());

DROP POLICY IF EXISTS p_notifications_access ON notifications_notification;
CREATE POLICY p_notifications_access ON notifications_notification
    FOR SELECT
    USING (
        user_id = erp_session_user_id()
        OR (user_id IS NULL AND target_role = erp_session_role())
    );

DROP POLICY IF EXISTS p_notifications_read_access ON notifications_read;
CREATE POLICY p_notifications_read_access ON notifications_read
    FOR ALL
    USING (user_id = erp_session_user_id())
    WITH CHECK (user_id = erp_session_user_id());

DROP POLICY IF EXISTS p_audit_log_access ON audit_log;
CREATE POLICY p_audit_log_access ON audit_log
    FOR SELECT
    USING (erp_session_role() = 'manager');

DROP POLICY IF EXISTS p_realtime_ticket_access ON realtime_ticket;
CREATE POLICY p_realtime_ticket_access ON realtime_ticket
    FOR ALL
    USING (user_id = erp_session_user_id() OR erp_session_role() = 'manager')
    WITH CHECK (user_id = erp_session_user_id() OR erp_session_role() = 'manager');

DROP POLICY IF EXISTS p_site_config_access ON accounts_site_config;
CREATE POLICY p_site_config_access ON accounts_site_config
    FOR SELECT
    USING (TRUE);

DROP POLICY IF EXISTS p_login_attempt_access ON accounts_login_attempt;
CREATE POLICY p_login_attempt_access ON accounts_login_attempt
    FOR SELECT
    USING (erp_session_role() = 'manager');

DROP POLICY IF EXISTS p_custom_field_value_access ON accounts_custom_field_value;
CREATE POLICY p_custom_field_value_access ON accounts_custom_field_value
    FOR ALL
    USING (erp_session_role() <> 'anonymous')
    WITH CHECK (erp_session_role() <> 'anonymous');

DROP POLICY IF EXISTS p_rule_run_access ON accounts_rule_run;
CREATE POLICY p_rule_run_access ON accounts_rule_run
    FOR SELECT
    USING (erp_session_role() = 'manager');

DROP POLICY IF EXISTS p_shop_list_access ON customers_shop_list;
CREATE POLICY p_shop_list_access ON customers_shop_list
    FOR ALL
    USING (
        erp_session_role() IN ('manager', 'accountant')
        OR (erp_session_role() = 'shop' AND customer_id = erp_session_customer_id())
    )
    WITH CHECK (
        erp_session_role() = 'shop' AND customer_id = erp_session_customer_id()
    );

DROP POLICY IF EXISTS p_push_sub_access ON notifications_push_subscription;
CREATE POLICY p_push_sub_access ON notifications_push_subscription
    FOR ALL
    USING (
        user_id = erp_session_user_id()
    )
    WITH CHECK (
        user_id = erp_session_user_id()
    );


DROP POLICY IF EXISTS p_vehicle_access ON deliveries_vehicle;
CREATE POLICY p_vehicle_access ON deliveries_vehicle
    FOR SELECT
    USING (erp_session_role() IN ('manager', 'accountant', 'warehouse', 'staff', 'rep'));

DROP POLICY IF EXISTS p_expense_access ON expenses_expense;
CREATE POLICY p_expense_access ON expenses_expense
    FOR ALL
    USING (erp_session_role() IN ('manager', 'accountant'))
    WITH CHECK (erp_session_role() IN ('manager', 'accountant'));

CREATE TABLE IF NOT EXISTS customers_visit (
    id          BIGSERIAL PRIMARY KEY,
    rep_id      BIGINT NOT NULL REFERENCES accounts_user(id) ON DELETE CASCADE,
    customer_id BIGINT NOT NULL REFERENCES customers_customer(id) ON DELETE CASCADE,
    day         DATE NOT NULL,
    at          TIMESTAMPTZ NOT NULL,
    outcome     VARCHAR(16) NOT NULL,
    reason      VARCHAR(20) NOT NULL DEFAULT '',
    note        TEXT NOT NULL DEFAULT '',
    lat         NUMERIC(9, 6) NULL,
    lng         NUMERIC(9, 6) NULL,
    distance_m  INTEGER NULL,
    far         BOOLEAN NULL,
    planned     BOOLEAN NOT NULL DEFAULT FALSE,
    order_id    BIGINT NULL REFERENCES orders_order(id) ON DELETE SET NULL,
    CONSTRAINT uniq_visit_rep_customer_day UNIQUE (rep_id, customer_id, day)
);

CREATE TABLE IF NOT EXISTS payments_cash_handover (
    id              BIGSERIAL PRIMARY KEY,
    number          VARCHAR(40) NOT NULL UNIQUE,
    rep_id          BIGINT NOT NULL REFERENCES accounts_user(id) ON DELETE RESTRICT,
    declared        BIGINT NOT NULL CHECK (declared > 0),
    received        BIGINT NULL,
    short           BIGINT NULL,
    status          VARCHAR(20) NOT NULL DEFAULT 'pending',
    note            TEXT NOT NULL DEFAULT '',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    confirmed_at    TIMESTAMPTZ NULL,
    confirmed_by_id BIGINT NULL REFERENCES accounts_user(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS catalog_stock_alert (
    id          BIGSERIAL PRIMARY KEY,
    customer_id BIGINT NOT NULL REFERENCES customers_customer(id) ON DELETE CASCADE,
    item_id     BIGINT NOT NULL REFERENCES catalog_item(id) ON DELETE CASCADE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uniq_stock_alert_customer_item UNIQUE (customer_id, item_id)
);

CREATE TABLE IF NOT EXISTS catalog_clearance (
    id            BIGSERIAL PRIMARY KEY,
    item_id       BIGINT NOT NULL REFERENCES catalog_item(id) ON DELETE CASCADE,
    batch_id      BIGINT NOT NULL REFERENCES inventory_batch(id) ON DELETE CASCADE,
    warehouse_id  BIGINT NOT NULL REFERENCES warehouses_warehouse(id) ON DELETE RESTRICT,
    percent       SMALLINT NOT NULL,
    qty           INTEGER NOT NULL,
    until         DATE NOT NULL,
    expiry_date   DATE NOT NULL,
    status        VARCHAR(20) NOT NULL DEFAULT 'active',
    created_by_id BIGINT NOT NULL REFERENCES accounts_user(id) ON DELETE RESTRICT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ended_at      TIMESTAMPTZ NULL
);

CREATE TABLE IF NOT EXISTS catalog_scheduled_price (
    id            BIGSERIAL PRIMARY KEY,
    start         DATE NOT NULL,
    items         JSONB NOT NULL DEFAULT '[]'::jsonb,
    percent       NUMERIC(6, 2) NULL,
    prices        JSONB NULL,
    status        VARCHAR(20) NOT NULL DEFAULT 'scheduled',
    note          TEXT NOT NULL DEFAULT '',
    created_by_id BIGINT NOT NULL REFERENCES accounts_user(id) ON DELETE RESTRICT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    applied_at    TIMESTAMPTZ NULL,
    told          INTEGER NOT NULL DEFAULT 0
);

COMMIT;
