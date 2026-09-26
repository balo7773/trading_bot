#!/usr/bin/env python3
"""
tests/test_execution_dispatcher.py

Verifies state transitions during order dispatch for fills, rejections, and invalid states.
"""
import pytest
from database.connection import get_connection, init_db
from database.repository import create_order, get_order_by_id
from execution.broker import MockBrokerClient
from execution.dispatcher import dispatch_order


def test_dispatch_order_successful_fill(tmp_path):
    """A successful broker response transitions the order to OPEN with fill metadata."""
    db_file = tmp_path / "dispatch_fill.db"
    init_db(db_file)
    order_id = "ORD-DISP-001"

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

        broker = MockBrokerClient(slippage=0.0002)
        result = dispatch_order(conn, client_order_id=order_id, broker=broker)

        assert result.success is True
        assert result.deal_id == f"DEAL-{order_id}"

    # Verify state in database
    with get_connection(db_file) as conn:
        order = get_order_by_id(conn, order_id)
        assert order["order_status"] == "OPEN"
        assert order["actual_fill_price"] == 1.0852
        assert order["deal_id"] == f"DEAL-{order_id}"
        assert order["opened_at"] is not None


def test_dispatch_order_broker_rejection(tmp_path):
    """A rejected execution transitions the order to ABORTED with the reject reason."""
    db_file = tmp_path / "dispatch_reject.db"
    init_db(db_file)
    order_id = "ORD-DISP-002"

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

        broker = MockBrokerClient(simulate_rejection=True, reject_reason="BROKER_REJECTED")
        result = dispatch_order(conn, client_order_id=order_id, broker=broker)

        assert result.success is False
        assert result.reject_reason == "BROKER_REJECTED"

    # Verify state in database
    with get_connection(db_file) as conn:
        order = get_order_by_id(conn, order_id)
        assert order["order_status"] == "ABORTED"
        assert order["abort_reason"] == "BROKER_REJECTED"


def test_dispatch_rejects_non_queued_order(tmp_path):
    """Attempting to dispatch an already OPEN order must raise ValueError."""
    db_file = tmp_path / "dispatch_invalid.db"
    init_db(db_file)
    order_id = "ORD-DISP-003"

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
        broker = MockBrokerClient()
        # First dispatch -> moves to OPEN
        dispatch_order(conn, client_order_id=order_id, broker=broker)

        # Second dispatch -> must raise ValueError
        with pytest.raises(ValueError, match="Must be 'QUEUED'"):
            dispatch_order(conn, client_order_id=order_id, broker=broker)
