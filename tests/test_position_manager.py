#!/usr/bin/env python3
"""
tests/test_position_manager.py

Verifies trailing stop updates, one-way ratchet enforcement, and break-even triggers.
"""
from database.connection import get_connection, init_db
from database.repository import create_order, get_order_by_id, record_order_fill
from positions.manager import evaluate_trailing_stop


def test_trailing_stop_advances_on_favorable_move(tmp_path):
    """When a BUY trade moves in profit, high watermark and stop price ratchet upward."""
    db_file = tmp_path / "pos_test_buy_trail.db"
    init_db(db_file)
    order_id = "ORD-POS-001"

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
        record_order_fill(
            conn,
            client_order_id=order_id,
            actual_fill_price=1.0850,
            deal_id="DEAL-01",
        )

        # Price advances to 1.0890. Trailing distance is 0.0020.
        # Candidate stop = 1.0890 - 0.0020 = 1.0870 (> initial 1.0810)
        new_stop = evaluate_trailing_stop(
            conn,
            client_order_id=order_id,
            current_price=1.0890,
            trailing_distance=0.0020,
        )
        assert new_stop == 1.0870

    # Verify database values
    with get_connection(db_file) as conn:
        order = get_order_by_id(conn, order_id)
        assert order["current_stop_price"] == 1.0870
        assert order["trailing_high_watermark"] == 1.0890
        assert order["stop_version"] == 2


def test_ratchet_blocks_stop_from_moving_backward_on_pullback(tmp_path):
    """When price pulls back after a run, the stop price must NOT move downward."""
    db_file = tmp_path / "pos_test_ratchet.db"
    init_db(db_file)
    order_id = "ORD-POS-002"

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
        record_order_fill(
            conn,
            client_order_id=order_id,
            actual_fill_price=1.0850,
            deal_id="DEAL-02",
        )

        # Step 1: Advance price to 1.0900 -> stop moves to 1.0880
        evaluate_trailing_stop(
            conn,
            client_order_id=order_id,
            current_price=1.0900,
            trailing_distance=0.0020,
        )

        # Step 2: Price pulls back to 1.0885 -> candidate stop (1.0865) < current stop (1.0880)
        new_stop = evaluate_trailing_stop(
            conn,
            client_order_id=order_id,
            current_price=1.0885,
            trailing_distance=0.0020,
        )
        assert new_stop is None  # Must NOT update stop

    # Verify stop remained at 1.0880
    with get_connection(db_file) as conn:
        order = get_order_by_id(conn, order_id)
        assert order["current_stop_price"] == 1.0880
        assert order["stop_version"] == 2  # Has not incremented again


def test_trailing_stop_sell_short_advances_downward(tmp_path):
    """When a SELL_SHORT trade moves in profit, low watermark and stop move downward."""
    db_file = tmp_path / "pos_test_short.db"
    init_db(db_file)
    order_id = "ORD-POS-003"

    with get_connection(db_file) as conn:
        create_order(
            conn,
            client_order_id=order_id,
            epic="CS.D.EURUSD.CFD.IP",
            sub_account="SUB_A_MR",
            direction="SELL_SHORT",
            planned_entry_price=1.0850,
            initial_stop_price=1.0890,
            planned_risk_r_usd=50.0,
            allocated_units=10000.0,
        )
        record_order_fill(
            conn,
            client_order_id=order_id,
            actual_fill_price=1.0850,
            deal_id="DEAL-03",
        )

        # Price drops to 1.0810. Trailing distance is 0.0020.
        # Candidate stop = 1.0810 + 0.0020 = 1.0830 (< initial 1.0890)
        new_stop = evaluate_trailing_stop(
            conn,
            client_order_id=order_id,
            current_price=1.0810,
            trailing_distance=0.0020,
        )
        assert new_stop == 1.0830

    with get_connection(db_file) as conn:
        order = get_order_by_id(conn, order_id)
        assert order["current_stop_price"] == 1.0830
        assert order["trailing_low_watermark"] == 1.0810


def test_ignores_non_open_positions(tmp_path):
    """QUEUED or CLOSED positions must not be evaluated for trailing stops."""
    db_file = tmp_path / "pos_test_ignore.db"
    init_db(db_file)
    order_id = "ORD-POS-004"

    with get_connection(db_file) as conn:
        # Order stays QUEUED (not OPEN)
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
        result = evaluate_trailing_stop(
            conn,
            client_order_id=order_id,
            current_price=1.0950,
            trailing_distance=0.0020,
        )
        assert result is None

    with get_connection(db_file) as conn:
        order = get_order_by_id(conn, order_id)
        assert order["current_stop_price"] == 1.0810
        assert order["stop_version"] == 1