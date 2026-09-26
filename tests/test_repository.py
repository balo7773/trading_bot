#!/usr/bin/env python3
"""
tests/test_repository.py

Verifies repository CRUD functions and state transition constraints.
"""

import sqlite3
import pytest

from database.connection import get_connection, init_db
from database.repository import (
    create_order,
    get_order_by_id,
    update_order_status,
)

def test_create_and_get_order(tmp_path):
    db_file = tmp_path / "repo_test.db"
    init_db(db_file)

    order_id = "ORD-REPO-001"

    # Step 1: Create order
    with get_connection(db_file) as conn:
        create_order(
            conn,
            client_order_id=order_id,
            epic="CS.D.EURUSD.CFD.IP",
            sub_account="SUB_A_MR",
            direction="BUY",
            planned_entry_price=1.0850,
            initial_stop_price=1.0810,
            planned_risk_r_usd=50.0,
            allocated_units=10000.0,
        )

    # Step 2: Retrieve and verify order
    with get_connection(db_file) as conn:
        row = get_order_by_id(conn, order_id)
        assert row is not None
        assert row["client_order_id"] == order_id
        assert row["order_status"] == "QUEUED"
        assert row["current_stop_price"] == 1.0810
        assert row["stop_version"] == 1
        assert row["abort_reason"] is None

import pytest


def test_update_order_status_success_and_failure(tmp_path):
    db_file = tmp_path / "repo_test_update.db"
    init_db(db_file)

    order_id = "ORD-REPO-002"

    # Create the baseline order
    with get_connection(db_file) as conn:
        create_order(
            conn,
            client_order_id=order_id,
            epic="CS.D.EURUSD.CFD.IP",
            sub_account="SUB_A_MR",
            direction="BUY",
            planned_entry_price=1.0850,
            initial_stop_price=1.0810,
            planned_risk_r_usd=50.0,
            allocated_units=10000.0,
        )

    # 1. Attempt invalid transition (ABORTED without abort_reason) -> Must raise IntegrityError
    with pytest.raises(sqlite3.IntegrityError):
        with get_connection(db_file) as conn:
            from database.repository import update_order_status
            update_order_status(conn, client_order_id=order_id, new_status="ABORTED")

    # 2. Valid transition (ABORTED with valid abort_reason) -> Must succeed
    with get_connection(db_file) as conn:
        from database.repository import update_order_status
        updated = update_order_status(
            conn,
            client_order_id=order_id,
            new_status="ABORTED",
            abort_reason="SPREAD_GATE_EXCEEDED",
        )
        assert updated is True

    # 3. Verify in database
    with get_connection(db_file) as conn:
        row = get_order_by_id(conn, order_id)
        assert row["order_status"] == "ABORTED"
        assert row["abort_reason"] == "SPREAD_GATE_EXCEEDED"

def test_record_order_fill(tmp_path):
    db_file = tmp_path / "repo_test_fill.db"
    init_db(db_file)

    order_id = "ORD-REPO-003"

    # Step 1: Create baseline order
    with get_connection(db_file) as conn:
        create_order(
            conn,
            client_order_id=order_id,
            epic="CS.D.EURUSD.CFD.IP",
            sub_account="SUB_A_MR",
            direction="BUY",
            planned_entry_price=1.0850,
            initial_stop_price=1.0810,
            planned_risk_r_usd=50.0,
            allocated_units=10000.0,
        )

    # Step 2: Record execution fill
    with get_connection(db_file) as conn:
        from database.repository import record_order_fill
        filled = record_order_fill(
            conn,
            client_order_id=order_id,
            actual_fill_price=1.0852,
            deal_id="DEAL-9988",
            deal_reference="REF-7766",
        )
        assert filled is True

    # Step 3: Verify updated state
    with get_connection(db_file) as conn:
        row = get_order_by_id(conn, order_id)
        assert row["order_status"] == "OPEN"
        assert row["actual_fill_price"] == 1.0852
        assert row["deal_id"] == "DEAL-9988"
        assert row["deal_reference"] == "REF-7766"
        assert row["opened_at"] is not None

def test_record_order_close(tmp_path):
    db_file = tmp_path / "repo_test_close.db"
    init_db(db_file)

    order_id = "ORD-REPO-004"

    # Step 1: Create baseline order
    with get_connection(db_file) as conn:
        create_order(
            conn,
            client_order_id=order_id,
            epic="CS.D.EURUSD.CFD.IP",
            sub_account="SUB_A_MR",
            direction="BUY",
            planned_entry_price=1.0850,
            initial_stop_price=1.0810,
            planned_risk_r_usd=50.0,
            allocated_units=10000.0,
        )

    # Step 2: Test invalid close_reason -> Must fail
    with pytest.raises(sqlite3.IntegrityError):
        with get_connection(db_file) as conn:
            from database.repository import record_order_close
            record_order_close(
                conn,
                client_order_id=order_id,
                close_reason="RANDOM_INVALID_REASON",
                realized_pnl_usd=100.0,
                realized_pnl_r=2.0,
            )

    # Step 3: Test valid close_reason -> Must succeed
    with get_connection(db_file) as conn:
        from database.repository import record_order_close
        closed = record_order_close(
            conn,
            client_order_id=order_id,
            close_reason="TARGET_BROKER",
            realized_pnl_usd=100.0,
            realized_pnl_r=2.0,
        )
        assert closed is True

    # Step 4: Verify closed state in database
    with get_connection(db_file) as conn:
        row = get_order_by_id(conn, order_id)
        assert row["order_status"] == "CLOSED"
        assert row["close_reason"] == "TARGET_BROKER"
        assert row["realized_pnl_usd"] == 100.0
        assert row["realized_pnl_r"] == 2.0
        assert row["closed_at"] is not None

def test_update_stop_price(tmp_path):
    db_file = tmp_path / "repo_test_stop.db"
    init_db(db_file)

    order_id = "ORD-REPO-005"

    # Step 1: Create baseline order (default stop_version starts at 1)
    with get_connection(db_file) as conn:
        create_order(
            conn,
            client_order_id=order_id,
            epic="CS.D.EURUSD.CFD.IP",
            sub_account="SUB_A_MR",
            direction="BUY",
            planned_entry_price=1.0850,
            initial_stop_price=1.0810,
            planned_risk_r_usd=50.0,
            allocated_units=10000.0,
        )

    # Step 2: Update the stop price
    with get_connection(db_file) as conn:
        from database.repository import update_stop_price
        updated = update_stop_price(
            conn,
            client_order_id=order_id,
            new_stop_price=1.0830,
            trailing_high_watermark=1.0890,
        )
        assert updated is True

    # Step 3: Check that stop price and version updated
    with get_connection(db_file) as conn:
        row = get_order_by_id(conn, order_id)
        assert row["current_stop_price"] == 1.0830
        assert row["stop_version"] == 2
        assert row["trailing_high_watermark"] == 1.0890

def test_get_orders_by_status(tmp_path):
    db_file = tmp_path / "repo_test_filter.db"
    init_db(db_file)

    with get_connection(db_file) as conn:
        from database.repository import record_order_close, record_order_fill

        # Order 1: EURUSD -> Stays QUEUED
        create_order(
            conn,
            client_order_id="ORD-STATUS-01",
            epic="CS.D.EURUSD.CFD.IP",
            sub_account="SUB_A_MR",
            direction="BUY",
            planned_entry_price=1.0850,
            initial_stop_price=1.0810,
            planned_risk_r_usd=50.0,
            allocated_units=10000.0,
        )

        # Order 2: GBPUSD -> Becomes OPEN
        create_order(
            conn,
            client_order_id="ORD-STATUS-02",
            epic="CS.D.GBPUSD.CFD.IP",
            sub_account="SUB_A_MR",
            direction="BUY",
            planned_entry_price=1.2650,
            initial_stop_price=1.2600,
            planned_risk_r_usd=50.0,
            allocated_units=10000.0,
        )
        record_order_fill(
            conn,
            client_order_id="ORD-STATUS-02",
            actual_fill_price=1.2652,
            deal_id="DEAL-02",
        )

        # Order 3: USDJPY -> Becomes CLOSED
        create_order(
            conn,
            client_order_id="ORD-STATUS-03",
            epic="CS.D.USDJPY.CFD.IP",
            sub_account="SUB_A_MR",
            direction="BUY",
            planned_entry_price=150.50,
            initial_stop_price=150.00,
            planned_risk_r_usd=50.0,
            allocated_units=10000.0,
        )
        record_order_close(
            conn,
            client_order_id="ORD-STATUS-03",
            close_reason="TARGET_BROKER",
            realized_pnl_usd=50.0,
            realized_pnl_r=1.0,
        )

    # Verify query results
    with get_connection(db_file) as conn:
        from database.repository import get_orders_by_status

        open_orders = get_orders_by_status(conn, ["OPEN"])
        assert len(open_orders) == 1
        assert open_orders[0]["client_order_id"] == "ORD-STATUS-02"

        active_orders = get_orders_by_status(conn, ["QUEUED", "OPEN"])
        assert len(active_orders) == 2
        order_ids = [row["client_order_id"] for row in active_orders]
        assert order_ids == ["ORD-STATUS-01", "ORD-STATUS-02"]

def test_get_order_by_deal_id_and_deal_ref(tmp_path):
    db_file = tmp_path / "repo_test_broker_lookups.db"
    init_db(db_file)

    order_id = "ORD-REPO-LOOKUP"
    deal_id = "BROKER-DEAL-1234"
    deal_ref = "BROKER-REF-5678"

    # Step 1: Create and fill the order with broker identifiers
    with get_connection(db_file) as conn:
        from database.repository import record_order_fill

        create_order(
            conn,
            client_order_id=order_id,
            epic="CS.D.EURUSD.CFD.IP",
            sub_account="SUB_A_MR",
            direction="BUY",
            planned_entry_price=1.0850,
            initial_stop_price=1.0810,
            planned_risk_r_usd=50.0,
            allocated_units=10000.0,
        )
        record_order_fill(
            conn,
            client_order_id=order_id,
            actual_fill_price=1.0851,
            deal_id=deal_id,
            deal_reference=deal_ref,
        )

    # Step 2: Test lookups
    with get_connection(db_file) as conn:
        from database.repository import get_order_by_deal_id, get_order_by_deal_ref

        # Query by deal_id
        row_by_id = get_order_by_deal_id(conn, deal_id)
        assert row_by_id is not None
        assert row_by_id["client_order_id"] == order_id

        # Query by deal_reference
        row_by_ref = get_order_by_deal_ref(conn, deal_ref)
        assert row_by_ref is not None
        assert row_by_ref["client_order_id"] == order_id

        # Query nonexistent values -> Must return None
        assert get_order_by_deal_id(conn, "NONEXISTENT_DEAL") is None
        assert get_order_by_deal_ref(conn, "NONEXISTENT_REF") is None

def test_block_epic_and_is_epic_blocked(tmp_path):
    db_file = tmp_path / "repo_test_epic_blocks.db"
    init_db(db_file)

    test_epic = "CS.D.EURUSD.CFD.IP"

    # Step 1: Epic should not be blocked initially
    with get_connection(db_file) as conn:
        from database.repository import is_epic_blocked, block_epic
        assert is_epic_blocked(conn, test_epic) is False

    # Step 2: Invalid reason must violate CHECK constraint
    with pytest.raises(sqlite3.IntegrityError):
        with get_connection(db_file) as conn:
            from database.repository import block_epic
            block_epic(conn, epic=test_epic, reason="NOT_A_REAL_REASON")

    # Step 3: Valid reason must succeed
    with get_connection(db_file) as conn:
        from database.repository import block_epic, is_epic_blocked
        block_epic(conn, epic=test_epic, reason="PERSISTENT_DESYNC")

    # Step 4: Verify block is active for test_epic, but not for other epics
    with get_connection(db_file) as conn:
        from database.repository import is_epic_blocked
        assert is_epic_blocked(conn, test_epic) is True
        assert is_epic_blocked(conn, "CS.D.GBPUSD.CFD.IP") is False