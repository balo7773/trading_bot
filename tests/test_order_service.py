#!/usr/bin/env python3
"""
tests/test_order_service.py

Verifies end-to-end signal screening and order creation workflow.
"""
from database.connection import get_connection, init_db
from database.repository import get_order_by_id
from orders.service import submit_signal_for_execution


def test_submit_signal_approved_and_stored(tmp_path):
    """A compliant signal passes risk gates and persists to SQLite as QUEUED."""
    db_file = tmp_path / "order_service_approved.db"
    init_db(db_file)
    order_id = "ORD-SRV-001"

    with get_connection(db_file) as conn:
        result = submit_signal_for_execution(
            conn,
            client_order_id=order_id,
            epic="CS.D.EURUSD.CFD.IP",
            sub_account="SUB_A_MR",
            direction="BUY",
            planned_entry_price=1.0850,
            stop_distance=0.0020,
            target_distance=0.0040,
            current_spread=0.8,
            max_allowed_spread=1.5,
            risk_budget_usd=100.0,
            point_value_per_unit=1.0,
        )

        assert result.success is True
        assert result.abort_reason is None

    # Verify database persistence
    with get_connection(db_file) as conn:
        saved_row = get_order_by_id(conn, order_id)
        assert saved_row is not None
        assert saved_row["order_status"] == "QUEUED"
        assert saved_row["initial_stop_price"] == 1.0830  # 1.0850 - 0.0020
        assert saved_row["target_price"] == 1.0890        # 1.0850 + 0.0040
        assert saved_row["allocated_units"] == 50000.0    # 100 / 0.0020


def test_submit_signal_rejected_by_risk_gate(tmp_path):
    """A non-compliant signal is rejected and does not write to the database."""
    db_file = tmp_path / "order_service_rejected.db"
    init_db(db_file)
    order_id = "ORD-SRV-002"

    with get_connection(db_file) as conn:
        result = submit_signal_for_execution(
            conn,
            client_order_id=order_id,
            epic="CS.D.EURUSD.CFD.IP",
            sub_account="SUB_A_MR",
            direction="BUY",
            planned_entry_price=1.0850,
            stop_distance=0.0020,
            target_distance=0.0040,
            current_spread=2.2,  # Exceeds max_allowed_spread of 1.5
            max_allowed_spread=1.5,
            risk_budget_usd=100.0,
        )

        assert result.success is False
        assert result.abort_reason == "SPREAD_GATE_EXCEEDED"

    # Verify no row was inserted into orders_and_positions
    with get_connection(db_file) as conn:
        saved_row = get_order_by_id(conn, order_id)
        assert saved_row is None