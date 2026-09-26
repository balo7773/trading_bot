#!/usr/bin/env python3
"""
tests/test_e2e_lifecycle.py

Validates the complete trade lifecycle from signal ingestion to final reconciliation:
1. Signal evaluation & pre-trade risk approval
2. Order persistence in QUEUED state
3. Dispatcher submission & broker fill (transition to OPEN)
4. Trailing stop updates on favorable price movement (one-way ratchet)
5. Remote broker exit detection & state reconciliation (transition to CLOSED)
"""
from database.connection import get_connection, init_db
from database.repository import get_order_by_id
from execution.broker import MockBrokerClient
from execution.dispatcher import dispatch_order
from orders.service import submit_signal_for_execution
from positions.manager import evaluate_trailing_stop
from reconciliation.reconciler import BrokerPosition, reconcile_positions


def test_full_trade_lifecycle_success(tmp_path):
    db_file = tmp_path / "e2e_lifecycle.db"
    init_db(db_file)

    order_id = "E2E-TRADE-001"
    epic = "CS.D.EURUSD.CFD.IP"
    sub_account = "SUB_A_MR"
    direction = "BUY"
    planned_entry = 1.0850
    stop_distance = 0.0020
    target_distance = 0.0040
    risk_budget = 100.0

    broker = MockBrokerClient(slippage=0.0001)

    # -------------------------------------------------------------
    # PHASE 1: Signal Ingestion & Pre-Trade Risk
    # -------------------------------------------------------------
    with get_connection(db_file) as conn:
        placement = submit_signal_for_execution(
            conn,
            client_order_id=order_id,
            epic=epic,
            sub_account=sub_account,
            direction=direction,
            planned_entry_price=planned_entry,
            stop_distance=stop_distance,
            target_distance=target_distance,
            current_spread=0.8,
            max_allowed_spread=1.5,
            risk_budget_usd=risk_budget,
            point_value_per_unit=1.0,
        )
        assert placement.success is True

    # Verify: Order is stored and QUEUED
    with get_connection(db_file) as conn:
        order = get_order_by_id(conn, order_id)
        assert order["order_status"] == "QUEUED"
        assert order["initial_stop_price"] == 1.0830
        assert order["current_stop_price"] == 1.0830
        assert order["stop_version"] == 1
        assert order["allocated_units"] == 50000.0

    # -------------------------------------------------------------
    # PHASE 2: Dispatch & Broker Fill
    # -------------------------------------------------------------
    with get_connection(db_file) as conn:
        exec_result = dispatch_order(conn, client_order_id=order_id, broker=broker)
        assert exec_result.success is True

    # Verify: Order is now OPEN with broker deal reference
    with get_connection(db_file) as conn:
        order = get_order_by_id(conn, order_id)
        assert order["order_status"] == "OPEN"
        assert order["actual_fill_price"] == 1.0851  # 1.0850 + 0.0001 slippage
        assert order["deal_id"] == f"DEAL-{order_id}"
        assert order["opened_at"] is not None

    # -------------------------------------------------------------
    # PHASE 3: Open Position Trailing Stop Management
    # -------------------------------------------------------------
    with get_connection(db_file) as conn:
        # Market advances favorably from 1.0851 to 1.0890
        # With trailing distance = 0.0020, candidate stop = 1.0890 - 0.0020 = 1.0870
        new_stop = evaluate_trailing_stop(
            conn,
            client_order_id=order_id,
            current_price=1.0890,
            trailing_distance=0.0020,
        )
        assert new_stop == 1.0870

        # Market pulls back to 1.0880 -> Ratchet must hold stop at 1.0870
        pullback_stop = evaluate_trailing_stop(
            conn,
            client_order_id=order_id,
            current_price=1.0880,
            trailing_distance=0.0020,
        )
        assert pullback_stop is None

    # Verify: Stop moved to 1.0870 and version incremented to 2
    with get_connection(db_file) as conn:
        order = get_order_by_id(conn, order_id)
        assert order["current_stop_price"] == 1.0870
        assert order["stop_version"] == 2
        assert order["trailing_high_watermark"] == 1.0890

    # -------------------------------------------------------------
    # PHASE 4: Position Closure & Reconciliation
    # -------------------------------------------------------------
    # Market hits the trailing stop at the broker.
    # Broker positions list is now empty.
    with get_connection(db_file) as conn:
        report = reconcile_positions(conn, broker_positions=[])
        assert order_id in report.closed_orders

    # Verify: Order transitions to CLOSED with timestamps
    with get_connection(db_file) as conn:
        order = get_order_by_id(conn, order_id)
        assert order["order_status"] == "CLOSED"
        assert order["close_reason"] == "STOP_BROKER"
        assert order["closed_at"] is not None