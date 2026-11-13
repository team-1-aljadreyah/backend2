"""PostgreSQL row-locking & concurrency verification tests (run when TEST_POSTGRES_URL is configured)."""
from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor

import pytest


def _require_pg_url() -> str:
    pg_url = os.getenv("TEST_POSTGRES_URL", "").strip()
    if not pg_url:
        pytest.skip("TEST_POSTGRES_URL is not set; skipping live PostgreSQL concurrency test.")
    return pg_url


@pytest.mark.postgres
def test_postgres_concurrent_fefo_lock_no_negative_stock():
    """20 concurrent workers deduct from one batch via SELECT ... FOR UPDATE without negative stock."""
    pg_url = _require_pg_url()
    import psycopg

    perm_table = f"pg_fefo_probe_{os.getpid()}"
    with psycopg.connect(pg_url, autocommit=True) as setup_conn:
        setup_conn.execute(f"DROP TABLE IF EXISTS {perm_table}")
        setup_conn.execute(
            f"CREATE TABLE {perm_table} (id INT PRIMARY KEY, qty_left INT NOT NULL CHECK (qty_left >= 0))"
        )
        setup_conn.execute(f"INSERT INTO {perm_table} (id, qty_left) VALUES (1, 25)")

    def _try_deduct(take: int) -> bool:
        with psycopg.connect(pg_url) as conn:
            with conn.cursor() as cur:
                cur.execute(f"SELECT qty_left FROM {perm_table} WHERE id = 1 FOR UPDATE")
                qty = cur.fetchone()[0]
                if qty < take:
                    conn.rollback()
                    return False
                cur.execute(f"UPDATE {perm_table} SET qty_left = qty_left - %s WHERE id = 1", (take,))
            conn.commit()
            return True

    try:
        with ThreadPoolExecutor(max_workers=20) as pool:
            outcomes = list(pool.map(_try_deduct, [4] * 20))
        assert outcomes.count(True) == 6
        assert outcomes.count(False) == 14
        with psycopg.connect(pg_url) as verify_conn:
            final_qty = verify_conn.execute(f"SELECT qty_left FROM {perm_table} WHERE id = 1").fetchone()[0]
            assert final_qty == 1
    finally:
        with psycopg.connect(pg_url, autocommit=True) as cleanup_conn:
            cleanup_conn.execute(f"DROP TABLE IF EXISTS {perm_table}")


@pytest.mark.postgres
def test_postgres_concurrent_orders_creation_no_oversell():
    """20 concurrent order creations lock the parent Item row (FOR UPDATE) so shelf capacity is never oversold."""
    pg_url = _require_pg_url()
    import psycopg

    suffix = os.getpid()
    item_tbl = f"pg_item_probe_{suffix}"
    res_tbl = f"pg_order_res_probe_{suffix}"
    with psycopg.connect(pg_url, autocommit=True) as setup_conn:
        setup_conn.execute(f"DROP TABLE IF EXISTS {res_tbl}, {item_tbl}")
        setup_conn.execute(f"CREATE TABLE {item_tbl} (id INT PRIMARY KEY, stock INT NOT NULL CHECK (stock >= 0))")
        setup_conn.execute(f"CREATE TABLE {res_tbl} (id SERIAL PRIMARY KEY, item_id INT NOT NULL, qty INT NOT NULL)")
        setup_conn.execute(f"INSERT INTO {item_tbl} (id, stock) VALUES (1, 12)")

    def _create_order(req_qty: int) -> bool:
        with psycopg.connect(pg_url) as conn:
            with conn.cursor() as cur:
                cur.execute(f"SELECT stock FROM {item_tbl} WHERE id = 1 FOR UPDATE")
                stock = cur.fetchone()[0]
                cur.execute(f"SELECT COALESCE(SUM(qty), 0) FROM {res_tbl} WHERE item_id = 1")
                reserved = cur.fetchone()[0]
                if stock - reserved < req_qty:
                    conn.rollback()
                    return False
                cur.execute(f"INSERT INTO {res_tbl} (item_id, qty) VALUES (1, %s)", (req_qty,))
            conn.commit()
            return True

    try:
        with ThreadPoolExecutor(max_workers=20) as pool:
            outcomes = list(pool.map(_create_order, [3] * 20))
        assert outcomes.count(True) == 4
        assert outcomes.count(False) == 16
        with psycopg.connect(pg_url) as verify_conn:
            total_reserved = verify_conn.execute(
                f"SELECT COALESCE(SUM(qty), 0) FROM {res_tbl} WHERE item_id = 1"
            ).fetchone()[0]
            assert total_reserved == 12
    finally:
        with psycopg.connect(pg_url, autocommit=True) as cleanup_conn:
            cleanup_conn.execute(f"DROP TABLE IF EXISTS {res_tbl}, {item_tbl}")


@pytest.mark.postgres
def test_postgres_concurrent_transfers_creation_no_oversell():
    """20 concurrent FEFO batch transfer allocations lock batches in ascending id order without over-transfer."""
    pg_url = _require_pg_url()
    import psycopg

    batch_tbl = f"pg_xfer_batch_probe_{os.getpid()}"
    with psycopg.connect(pg_url, autocommit=True) as setup_conn:
        setup_conn.execute(f"DROP TABLE IF EXISTS {batch_tbl}")
        setup_conn.execute(
            f"CREATE TABLE {batch_tbl} (id INT PRIMARY KEY, item_id INT NOT NULL, qty_left INT NOT NULL CHECK (qty_left >= 0))"
        )
        setup_conn.execute(f"INSERT INTO {batch_tbl} (id, item_id, qty_left) VALUES (1, 1, 7), (2, 1, 8)")

    def _allocate_transfer(need: int) -> bool:
        with psycopg.connect(pg_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT id, qty_left FROM {batch_tbl} WHERE item_id = 1 AND qty_left > 0 ORDER BY id FOR UPDATE"
                )
                rows = cur.fetchall()
                total_avail = sum(r[1] for r in rows)
                if total_avail < need:
                    conn.rollback()
                    return False
                rem = need
                for bid, qty_left in rows:
                    if rem <= 0:
                        break
                    take = min(rem, qty_left)
                    cur.execute(f"UPDATE {batch_tbl} SET qty_left = qty_left - %s WHERE id = %s", (take, bid))
                    rem -= take
            conn.commit()
            return True

    try:
        with ThreadPoolExecutor(max_workers=20) as pool:
            outcomes = list(pool.map(_allocate_transfer, [4] * 20))
        # Total capacity is 15 across two batches; 3 transfers of 4 succeed (12 deducted), 3 remaining cannot satisfy 4
        assert outcomes.count(True) == 3
        assert outcomes.count(False) == 17
        with psycopg.connect(pg_url) as verify_conn:
            remaining = verify_conn.execute(f"SELECT COALESCE(SUM(qty_left), 0) FROM {batch_tbl}").fetchone()[0]
            assert remaining == 3
    finally:
        with psycopg.connect(pg_url, autocommit=True) as cleanup_conn:
            cleanup_conn.execute(f"DROP TABLE IF EXISTS {batch_tbl}")


@pytest.mark.postgres
def test_postgres_concurrent_supply_order_receipt_no_over_receiving():
    """20 concurrent receipt reconciliations lock SupplyOrderLine FOR UPDATE so received_qty never exceeds ordered qty."""
    pg_url = _require_pg_url()
    import psycopg

    line_tbl = f"pg_so_line_probe_{os.getpid()}"
    with psycopg.connect(pg_url, autocommit=True) as setup_conn:
        setup_conn.execute(f"DROP TABLE IF EXISTS {line_tbl}")
        setup_conn.execute(
            f"""
            CREATE TABLE {line_tbl} (
                id INT PRIMARY KEY,
                qty INT NOT NULL,
                received_qty INT NOT NULL DEFAULT 0,
                CHECK (received_qty >= 0 AND received_qty <= qty)
            )
            """
        )
        setup_conn.execute(f"INSERT INTO {line_tbl} (id, qty, received_qty) VALUES (1, 10, 0)")

    def _receive(chunk: int) -> bool:
        with psycopg.connect(pg_url) as conn:
            with conn.cursor() as cur:
                cur.execute(f"SELECT qty, received_qty FROM {line_tbl} WHERE id = 1 FOR UPDATE")
                qty, received = cur.fetchone()
                if received + chunk > qty:
                    conn.rollback()
                    return False
                cur.execute(
                    f"UPDATE {line_tbl} SET received_qty = received_qty + %s WHERE id = 1",
                    (chunk,),
                )
            conn.commit()
            return True

    try:
        with ThreadPoolExecutor(max_workers=20) as pool:
            outcomes = list(pool.map(_receive, [3] * 20))
        assert outcomes.count(True) == 3
        assert outcomes.count(False) == 17
        with psycopg.connect(pg_url) as verify_conn:
            received = verify_conn.execute(f"SELECT received_qty FROM {line_tbl} WHERE id = 1").fetchone()[0]
            assert received == 9
    finally:
        with psycopg.connect(pg_url, autocommit=True) as cleanup_conn:
            cleanup_conn.execute(f"DROP TABLE IF EXISTS {line_tbl}")


@pytest.mark.postgres
def test_postgres_concurrent_payments_fifo_allocation():
    """20 concurrent FIFO payment allocations lock customer invoices ORDER BY id FOR UPDATE without over-allocation."""
    pg_url = _require_pg_url()
    import psycopg

    suffix = os.getpid()
    inv_tbl = f"pg_inv_fifo_probe_{suffix}"
    alloc_tbl = f"pg_pay_alloc_probe_{suffix}"
    with psycopg.connect(pg_url, autocommit=True) as setup_conn:
        setup_conn.execute(f"DROP TABLE IF EXISTS {alloc_tbl}, {inv_tbl}")
        setup_conn.execute(
            f"""
            CREATE TABLE {inv_tbl} (
                id INT PRIMARY KEY,
                customer_id INT NOT NULL,
                total BIGINT NOT NULL,
                paid BIGINT NOT NULL DEFAULT 0,
                CHECK (paid >= 0 AND paid <= total)
            )
            """
        )
        setup_conn.execute(
            f"CREATE TABLE {alloc_tbl} (id SERIAL PRIMARY KEY, invoice_id INT NOT NULL, amount BIGINT NOT NULL)"
        )
        setup_conn.execute(f"INSERT INTO {inv_tbl} (id, customer_id, total, paid) VALUES (1, 1, 6000, 0), (2, 1, 6000, 0)")

    def _allocate(amount: int) -> int:
        with psycopg.connect(pg_url) as conn:
            allocated_here = 0
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT id, total, paid FROM {inv_tbl} WHERE customer_id = 1 AND paid < total ORDER BY id FOR UPDATE"
                )
                invs = cur.fetchall()
                rem = amount
                for inv_id, total, paid in invs:
                    if rem <= 0:
                        break
                    due = total - paid
                    take = min(rem, due)
                    if take > 0:
                        cur.execute(f"UPDATE {inv_tbl} SET paid = paid + %s WHERE id = %s", (take, inv_id))
                        cur.execute(
                            f"INSERT INTO {alloc_tbl} (invoice_id, amount) VALUES (%s, %s)",
                            (inv_id, take),
                        )
                        allocated_here += take
                        rem -= take
            conn.commit()
            return allocated_here

    try:
        with ThreadPoolExecutor(max_workers=20) as pool:
            allocated = list(pool.map(_allocate, [4000] * 20))
        assert sum(allocated) == 12000
        with psycopg.connect(pg_url) as verify_conn:
            paid_rows = verify_conn.execute(f"SELECT id, total, paid FROM {inv_tbl} ORDER BY id").fetchall()
            assert paid_rows == [(1, 6000, 6000), (2, 6000, 6000)]
            alloc_sum = verify_conn.execute(f"SELECT COALESCE(SUM(amount), 0) FROM {alloc_tbl}").fetchone()[0]
            assert alloc_sum == 12000
    finally:
        with psycopg.connect(pg_url, autocommit=True) as cleanup_conn:
            cleanup_conn.execute(f"DROP TABLE IF EXISTS {alloc_tbl}, {inv_tbl}")


@pytest.mark.postgres
def test_postgres_concurrent_delivery_dispatch_and_pin_lockout():
    """20 concurrent delivery dispatch and wrong-PIN attempts serialize cleanly without lost updates or double dispatch."""
    pg_url = _require_pg_url()
    import psycopg

    dlv_tbl = f"pg_dlv_probe_{os.getpid()}"
    with psycopg.connect(pg_url, autocommit=True) as setup_conn:
        setup_conn.execute(f"DROP TABLE IF EXISTS {dlv_tbl}")
        setup_conn.execute(
            f"""
            CREATE TABLE {dlv_tbl} (
                id INT PRIMARY KEY,
                status TEXT NOT NULL,
                pin_attempts INT NOT NULL DEFAULT 0,
                locked BOOLEAN NOT NULL DEFAULT FALSE
            )
            """
        )
        setup_conn.execute(f"INSERT INTO {dlv_tbl} (id, status, pin_attempts, locked) VALUES (1, 'pending', 0, FALSE)")

    def _try_dispatch(_: int) -> bool:
        with psycopg.connect(pg_url) as conn:
            with conn.cursor() as cur:
                cur.execute(f"SELECT status FROM {dlv_tbl} WHERE id = 1 FOR UPDATE")
                status = cur.fetchone()[0]
                if status != "pending":
                    conn.rollback()
                    return False
                cur.execute(f"UPDATE {dlv_tbl} SET status = 'out' WHERE id = 1")
            conn.commit()
            return True

    def _wrong_pin(_: int) -> bool:
        with psycopg.connect(pg_url) as conn:
            with conn.cursor() as cur:
                cur.execute(f"SELECT pin_attempts, locked FROM {dlv_tbl} WHERE id = 1 FOR UPDATE")
                attempts, locked = cur.fetchone()
                if locked:
                    conn.rollback()
                    return False
                new_attempts = attempts + 1
                new_locked = new_attempts >= 5
                cur.execute(
                    f"UPDATE {dlv_tbl} SET pin_attempts = %s, locked = %s WHERE id = 1",
                    (new_attempts, new_locked),
                )
            conn.commit()
            return True

    try:
        with ThreadPoolExecutor(max_workers=20) as pool:
            dispatch_outcomes = list(pool.map(_try_dispatch, range(20)))
            pin_outcomes = list(pool.map(_wrong_pin, range(20)))
        assert dispatch_outcomes.count(True) == 1
        assert dispatch_outcomes.count(False) == 19
        assert pin_outcomes.count(True) == 5
        assert pin_outcomes.count(False) == 15
        with psycopg.connect(pg_url) as verify_conn:
            row = verify_conn.execute(f"SELECT status, pin_attempts, locked FROM {dlv_tbl} WHERE id = 1").fetchone()
            assert row == ("out", 5, True)
    finally:
        with psycopg.connect(pg_url, autocommit=True) as cleanup_conn:
            cleanup_conn.execute(f"DROP TABLE IF EXISTS {dlv_tbl}")


@pytest.mark.postgres
def test_postgres_realtime_ticket_single_use_race():
    """20 concurrent ticket redemptions guarantee single-use redemption via atomic UPDATE ... WHERE used_at IS NULL."""
    pg_url = _require_pg_url()
    import psycopg

    tbl = f"pg_rt_ticket_probe_{os.getpid()}"
    with psycopg.connect(pg_url, autocommit=True) as setup_conn:
        setup_conn.execute(f"DROP TABLE IF EXISTS {tbl}")
        setup_conn.execute(f"CREATE TABLE {tbl} (token_hash TEXT PRIMARY KEY, used_at TIMESTAMPTZ NULL)")
        setup_conn.execute(f"INSERT INTO {tbl} (token_hash, used_at) VALUES ('hash_1', NULL)")

    def _redeem(_: int) -> bool:
        with psycopg.connect(pg_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE {tbl} SET used_at = NOW() WHERE token_hash = 'hash_1' AND used_at IS NULL RETURNING token_hash"
                )
                won = cur.fetchone() is not None
            conn.commit()
            return won

    try:
        with ThreadPoolExecutor(max_workers=20) as pool:
            results = list(pool.map(_redeem, range(20)))
        assert results.count(True) == 1
        assert results.count(False) == 19
    finally:
        with psycopg.connect(pg_url, autocommit=True) as cleanup_conn:
            cleanup_conn.execute(f"DROP TABLE IF EXISTS {tbl}")
