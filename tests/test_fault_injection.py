"""
tests/test_fault_injection.py
Fault-Injection Certification Suite (FI-01 through FI-09).
Implements Master Spec V4.1 Sections 7 and 8.
"""
import json
import sqlite3
import pytest
from pathlib import Path
from unittest.mock import MagicMock

from config.enums import (
    AbortReason,
    AuditEventType,
    BlockReason,
    CloseReason,
    OrderStatus,
    SubAccount,
    TradeDirection,
)
from execution.order_manager import execute_order
from monitoring.circuit_breaker import (
    audit_consecutive_timeouts,
    get_circuit_breaker_status,
)
from monitoring.reconciliation import reconcile_positions
from monitoring.recovery import run_startup_recovery

NOW = 1700000000000


@pytest.fixture
def db_conn():
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON;")
    schema_path = Path("database/schema.sql")
    with open(schema_path, "r") as f:
        conn.executescript(f.read())

    # Seed Parent Hierarchy
    conn.execute(
        "INSERT INTO screening_runs (run_id, run_date, run_status, started_at, config_snapshot_json) "
        "VALUES ('run-fi', '2026-09-24', 'COMPLETED', 1700000000, '{}')"
    )
    conn.execute(
        "INSERT INTO weekly_watchlist (watchlist_id, run_id, epic, sector, region, assigned_engine, "
        "permitted_direction, sunday_composite_rank, sunday_composite_score, atr_pct_60d, created_at) "
        "VALUES ('wl-fi', 'run-fi', 'AAPL', 'Technology', 'US', 'MEAN_REVERSION', 'LONG_ONLY', 1, 1.0, 1.5, 1700000000)"
    )
    conn.execute(
        "INSERT INTO daily_signals (signal_id, watchlist_id, epic, engine, direction, signal_date, "
        "signal_close_price, structural_stop_price, target_price, initial_rr_ratio, stop_distance_pct, "
        "provisional_priority, signal_status, reclaim_deadline_at, created_at) "
        "VALUES ('sig-fi-1', 'wl-fi', 'AAPL', 'MEAN_REVERSION', 'BUY', '2026-09-24', 150.0, 145.0, 160.0, 2.0, 3.33, 2.0, 'PROMOTED', 1700003600, 1700000000)"
    )
    conn.commit()
    yield conn
    conn.close()


# ============================================================================
# FI-01: POST /positions Timeout Leaves Durable SUBMITTING
# ============================================================================
def test_fi_01_create_position_timeout_preserves_submitting(db_conn):
    mock_broker = MagicMock()
    mock_broker.fetch_open_positions.return_value = []
    mock_broker.create_position.side_effect = TimeoutError("HTTP 504 Gateway Timeout on POST /positions")

    res = execute_order(
        conn=db_conn,
        broker_client=mock_broker,
        client_order_id="ORD-FI-01",
        signal_id="sig-fi-1",
        epic="AAPL",
        sub_account=SubAccount.SUB_A_MR,
        direction=TradeDirection.BUY,
        planned_entry_price=150.0,
        executable_entry_price=150.0,
        initial_stop_price=145.0,
        target_price=160.0,
        planned_risk_r_usd=100.0,
        allocated_units=20.0,
        margin_used_usd=600.0,
    )

    assert res.order_status == OrderStatus.SUBMITTING
    assert res.deal_reference is None

    row = db_conn.execute("SELECT order_status FROM orders_and_positions WHERE client_order_id = 'ORD-FI-01'").fetchone()
    assert row[0] == "SUBMITTING"


# ============================================================================
# FI-02: GET /confirms Timeout Preserves Durable SUBMITTED
# ============================================================================
def test_fi_02_confirm_timeout_preserves_submitted(db_conn):
    mock_broker = MagicMock()
    mock_broker.fetch_open_positions.return_value = []
    mock_broker.create_position.return_value = "deal-ref-fi-02"
    mock_broker.confirm_order.side_effect = TimeoutError("Connection reset during GET /confirms")

    res = execute_order(
        conn=db_conn,
        broker_client=mock_broker,
        client_order_id="ORD-FI-02",
        signal_id="sig-fi-1",
        epic="AAPL",
        sub_account=SubAccount.SUB_A_MR,
        direction=TradeDirection.BUY,
        planned_entry_price=150.0,
        executable_entry_price=150.0,
        initial_stop_price=145.0,
        target_price=160.0,
        planned_risk_r_usd=100.0,
        allocated_units=20.0,
        margin_used_usd=600.0,
    )

    assert res.order_status == OrderStatus.SUBMITTED
    assert res.deal_reference == "deal-ref-fi-02"

    row = db_conn.execute("SELECT order_status, deal_reference FROM orders_and_positions WHERE client_order_id = 'ORD-FI-02'").fetchone()
    assert row[0] == "SUBMITTED"
    assert row[1] == "deal-ref-fi-02"


# ============================================================================
# FI-03: Immediate Broker Rejection Transition
# ============================================================================
def test_fi_03_broker_rejection_marks_rejected(db_conn):
    mock_broker = MagicMock()
    mock_broker.fetch_open_positions.return_value = []
    mock_broker.create_position.return_value = "deal-ref-fi-03"

    confirm = MagicMock()
    confirm.status = "REJECTED"
    confirm.reject_reason = "INSUFFICIENT_FUNDS"
    mock_broker.confirm_order.return_value = confirm

    res = execute_order(
        conn=db_conn,
        broker_client=mock_broker,
        client_order_id="ORD-FI-03",
        signal_id="sig-fi-1",
        epic="AAPL",
        sub_account=SubAccount.SUB_A_MR,
        direction=TradeDirection.BUY,
        planned_entry_price=150.0,
        executable_entry_price=150.0,
        initial_stop_price=145.0,
        target_price=160.0,
        planned_risk_r_usd=100.0,
        allocated_units=20.0,
        margin_used_usd=600.0,
    )

    assert res.order_status == OrderStatus.REJECTED
    assert res.abort_reason == AbortReason.BROKER_REJECTED

    row = db_conn.execute("SELECT order_status, abort_reason FROM orders_and_positions WHERE client_order_id = 'ORD-FI-03'").fetchone()
    assert row[0] == "REJECTED"
    assert row[1] == AbortReason.BROKER_REJECTED.value


# ============================================================================
# FI-04: Crash / Reboot Recovery Adopts Late-Fill Orphan
# ============================================================================
def test_fi_04_startup_recovery_adopts_unconfirmed_fill(db_conn):
    # Simulates state after VPS killed mid-flight while in SUBMITTING
    db_conn.execute(
        """
        INSERT INTO orders_and_positions (
            client_order_id, signal_id, epic, sub_account, direction,
            order_status, planned_entry_price, executable_entry_price,
            initial_stop_price, current_stop_price, target_price,
            planned_risk_r_usd, allocated_units, margin_used_usd,
            created_at, updated_at
        ) VALUES ('ORD-FI-04', 'sig-fi-1', 'AAPL', 'SUB_A_MR', 'BUY', 'SUBMITTING',
                  150.0, 150.0, 145.0, 145.0, 160.0, 100.0, 20.0, 600.0, ?, ?)
        """,
        (NOW - 60_000, NOW - 60_000),
    )
    db_conn.commit()

    # Live broker shows position filled while daemon was down
    mock_broker = MagicMock()
    mock_broker.fetch_open_positions.return_value = [{
        "epic": "AAPL", "deal_id": "DEAL-RECOVERED", "direction": "BUY", "size": 20.0, "level": 150.10
    }]

    report = run_startup_recovery(conn=db_conn, broker_client=mock_broker, current_time_ms=NOW)
    assert "ORD-FI-04" in report.submitting_adopted_open

    row = db_conn.execute("SELECT order_status, deal_id, actual_fill_price FROM orders_and_positions WHERE client_order_id = 'ORD-FI-04'").fetchone()
    assert row[0] == "OPEN"
    assert row[1] == "DEAL-RECOVERED"
    assert row[2] == 150.10


# ============================================================================
# FI-05: Broker Eventual Consistency Grace Period
# ============================================================================
def test_fi_05_reconciliation_skips_young_ghost_within_grace(db_conn):
    # Position opened 2 minutes ago (< 5 min grace window)
    db_conn.execute(
        """
        INSERT INTO orders_and_positions (
            client_order_id, signal_id, epic, sub_account, direction,
            order_status, planned_entry_price, executable_entry_price,
            actual_fill_price, initial_stop_price, current_stop_price,
            target_price, planned_risk_r_usd, allocated_units, margin_used_usd,
            opened_at, created_at, updated_at
        ) VALUES ('ORD-FI-05', 'sig-fi-1', 'AAPL', 'SUB_A_MR', 'BUY', 'OPEN',
                  150.0, 150.0, 150.0, 145.0, 145.0, 160.0, 100.0, 20.0, 600.0,
                  ?, ?, ?)
        """,
        (NOW - 120_000, NOW - 120_000, NOW - 120_000),
    )
    db_conn.commit()

    # Broker API briefly returns empty /positions array due to replication lag
    mock_broker = MagicMock()
    mock_broker.fetch_open_positions.return_value = []

    report = reconcile_positions(conn=db_conn, broker_client=mock_broker, current_time_ms=NOW)
    assert "ORD-FI-05" not in report.ghost_exits_closed

    row = db_conn.execute("SELECT order_status FROM orders_and_positions WHERE client_order_id = 'ORD-FI-05'").fetchone()
    assert row[0] == "OPEN"  # Preserved!


# ============================================================================
# FI-06: Stop Amendment Cooldown Suppresses Race Conditions
# ============================================================================
def test_fi_06_in_flight_mutation_cooldown_prevents_repair(db_conn):
    # Order updated 10 seconds ago (< 30s cooldown window)
    db_conn.execute(
        """
        INSERT INTO orders_and_positions (
            client_order_id, signal_id, epic, sub_account, direction,
            order_status, planned_entry_price, executable_entry_price,
            actual_fill_price, initial_stop_price, current_stop_price,
            target_price, planned_risk_r_usd, allocated_units, margin_used_usd,
            opened_at, created_at, updated_at
        ) VALUES ('ORD-FI-06', 'sig-fi-1', 'AAPL', 'SUB_A_MR', 'BUY', 'OPEN',
                  150.0, 150.0, 150.0, 145.0, 145.0, 160.0, 100.0, 20.0, 600.0,
                  ?, ?, ?)
        """,
        (NOW - 600_000, NOW - 600_000, NOW - 10_000),
    )
    db_conn.commit()

    # Broker shows looser stop (amendment hasn't arrived at broker yet)
    mock_broker = MagicMock()
    mock_broker.fetch_open_positions.return_value = [{
        "epic": "AAPL", "deal_id": "DEAL-FI-06", "direction": "BUY", "size": 20.0, "stop_level": 140.0
    }]

    report = reconcile_positions(conn=db_conn, broker_client=mock_broker, current_time_ms=NOW)
    assert "ORD-FI-06" not in report.stops_repaired
    mock_broker.update_position.assert_not_called()


# ============================================================================
# FI-07: Consecutive Timeouts Trip Circuit Breaker and Lock Gates
# ============================================================================
def test_fi_07_three_consecutive_timeouts_trip_circuit_breaker(db_conn):
    # Log 3 consecutive API timeouts within the 1-hour recency window
    for i in range(3):
        db_conn.execute(
            """
            INSERT INTO system_audit_log (timestamp, event_type, discrepancy_detected, details_json)
            VALUES (?, ?, 1, '{}')
            """,
            (NOW - (3 - i) * 1000, AuditEventType.API_TIMEOUT.value),
        )
    db_conn.commit()

    tripped = audit_consecutive_timeouts(conn=db_conn, current_time_ms=NOW)
    assert tripped is True

    status = get_circuit_breaker_status(db_conn)
    assert status.is_active is True
    assert status.reason == "CONSECUTIVE_TIMEOUTS"

    # Upstream Order Manager must block new entries immediately
    mock_broker = MagicMock()
    mock_broker.fetch_open_positions.return_value = []
    res = execute_order(
        conn=db_conn,
        broker_client=mock_broker,
        client_order_id="ORD-FI-07-BLOCKED",
        signal_id="sig-fi-1",
        epic="AAPL",
        sub_account=SubAccount.SUB_A_MR,
        direction=TradeDirection.BUY,
        planned_entry_price=150.0,
        executable_entry_price=150.0,
        initial_stop_price=145.0,
        target_price=160.0,
        planned_risk_r_usd=100.0,
        allocated_units=20.0,
        margin_used_usd=600.0,
    )
    assert res.order_status == OrderStatus.ABORTED
    assert res.abort_reason == AbortReason.CIRCUIT_BREAKER
    mock_broker.create_position.assert_not_called()


# ============================================================================
# FI-08: Partial Close Size Downscaling
# ============================================================================
def test_fi_08_partial_close_scales_units_and_margin(db_conn):
    db_conn.execute(
        """
        INSERT INTO orders_and_positions (
            client_order_id, signal_id, epic, sub_account, direction,
            order_status, planned_entry_price, executable_entry_price,
            actual_fill_price, initial_stop_price, current_stop_price,
            target_price, planned_risk_r_usd, allocated_units, margin_used_usd,
            opened_at, created_at, updated_at
        ) VALUES ('ORD-FI-08', 'sig-fi-1', 'AAPL', 'SUB_A_MR', 'BUY', 'OPEN',
                  150.0, 150.0, 150.0, 145.0, 145.0, 160.0, 100.0, 20.0, 600.0,
                  ?, ?, ?)
        """,
        (NOW - 600_000, NOW - 600_000, NOW - 60_000),
    )
    db_conn.commit()

    # Broker position reduced to 10 units
    mock_broker = MagicMock()
    mock_broker.fetch_open_positions.return_value = [{
        "epic": "AAPL", "deal_id": "DEAL-FI-08", "direction": "BUY", "size": 10.0, "stop_level": 145.0
    }]

    report = reconcile_positions(conn=db_conn, broker_client=mock_broker, current_time_ms=NOW)
    assert "ORD-FI-08" in report.partial_closes_synced

    row = db_conn.execute("SELECT allocated_units, margin_used_usd FROM orders_and_positions WHERE client_order_id = 'ORD-FI-08'").fetchone()
    assert row[0] == 10.0
    assert row[1] == 300.0  # Margin proportionally halved


# ============================================================================
# FI-09: Unowned Manual Position Containment (Quarantine Block)
# ============================================================================
def test_fi_09_unowned_broker_position_quarantines_epic(db_conn):
    # Broker shows an unmanaged manual trade on TSLA
    mock_broker = MagicMock()
    mock_broker.fetch_open_positions.return_value = [{
        "epic": "TSLA", "deal_id": "DEAL-MANUAL", "direction": "BUY", "size": 15.0, "stop_level": 250.0
    }]

    report = reconcile_positions(conn=db_conn, broker_client=mock_broker, current_time_ms=NOW)
    assert "TSLA" in report.blocked_foreign_epics

    # Verify epic is blocked in epic_blocks
    block = db_conn.execute("SELECT reason, blocked_at FROM epic_blocks WHERE epic = 'TSLA' AND unblocked_at IS NULL").fetchone()
    assert block is not None
    assert block[0] == BlockReason.MANUAL_POSITION_EXISTS.value
