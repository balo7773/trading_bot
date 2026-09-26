"""
tests/test_order_manager.py
Unit tests verifying Write-Ahead Handshake, Pre-Submission Gates,
and Fault-Tolerant State Durability per Master Spec V4.1.
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
    OrderStatus,
    SubAccount,
    TradeDirection,
)
from execution.order_manager import execute_order


@pytest.fixture
def db_conn():
    """Provides an in-memory SQLite database initialized with schema.sql."""
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON;")
    
    schema_path = Path("database/schema.sql")
    with open(schema_path, "r") as f:
        conn.executescript(f.read())

    # Seed Parent Records for foreign key constraints
    conn.execute(
        "INSERT INTO screening_runs (run_id, run_date, run_status, started_at, config_snapshot_json) "
        "VALUES ('run-ord', '2026-09-24', 'COMPLETED', 1700000000, '{}')"
    )
    conn.execute(
        "INSERT INTO weekly_watchlist (watchlist_id, run_id, epic, sector, region, assigned_engine, "
        "permitted_direction, sunday_composite_rank, sunday_composite_score, atr_pct_60d, created_at) "
        "VALUES ('wl-ord', 'run-ord', 'AAPL', 'Technology', 'US', 'MEAN_REVERSION', 'LONG_ONLY', 1, 1.0, 1.5, 1700000000)"
    )
    conn.execute(
        "INSERT INTO daily_signals (signal_id, watchlist_id, epic, engine, direction, signal_date, "
        "signal_close_price, structural_stop_price, target_price, initial_rr_ratio, stop_distance_pct, "
        "provisional_priority, signal_status, reclaim_deadline_at, created_at) "
        "VALUES ('sig-ord-1', 'wl-ord', 'AAPL', 'MEAN_REVERSION', 'BUY', '2026-09-24', 150.0, 145.0, 160.0, 2.0, 3.33, 2.0, 'PROMOTED', 1700003600, 1700000000)"
    )
    conn.commit()
    yield conn
    conn.close()


# ============================================================================
# 1. Pre-Submission Gate Tests
# ============================================================================

def test_pre_submission_gate_active_block_aborts(db_conn):
    # Insert active block for AAPL
    db_conn.execute(
        "INSERT INTO epic_blocks (epic, reason, blocked_at) VALUES ('AAPL', 'PERSISTENT_DESYNC', 1700000000)"
    )
    db_conn.commit()

    mock_client = MagicMock()
    mock_client.fetch_open_positions.return_value = []

    res = execute_order(
        conn=db_conn,
        broker_client=mock_client,
        client_order_id="ORD-001",
        signal_id="sig-ord-1",
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
    assert res.abort_reason == AbortReason.AMBIGUOUS_BROKER_MATCH
    mock_client.create_position.assert_not_called()


def test_pre_submission_gate_circuit_breaker_tripped(db_conn):
    # Trip Circuit Breaker
    db_conn.execute("UPDATE system_state SET value = '1' WHERE key = 'circuit_breaker_active'")
    db_conn.commit()

    mock_client = MagicMock()
    mock_client.fetch_open_positions.return_value = []

    res = execute_order(
        conn=db_conn,
        broker_client=mock_client,
        client_order_id="ORD-002",
        signal_id="sig-ord-1",
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
    mock_client.create_position.assert_not_called()


def test_pre_submission_gate_foreign_manual_position_blocks_and_aborts(db_conn):
    # Broker shows an unowned position on AAPL
    mock_client = MagicMock()
    mock_pos = MagicMock()
    mock_pos.epic = "AAPL"
    mock_client.fetch_open_positions.return_value = [mock_pos]

    res = execute_order(
        conn=db_conn,
        broker_client=mock_client,
        client_order_id="ORD-003",
        signal_id="sig-ord-1",
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
    assert res.abort_reason == AbortReason.AMBIGUOUS_BROKER_MATCH

    # Verify epic_blocks now has MANUAL_POSITION_EXISTS
    block = db_conn.execute("SELECT reason FROM epic_blocks WHERE epic = 'AAPL' AND unblocked_at IS NULL").fetchone()
    assert block is not None
    assert block[0] == BlockReason.MANUAL_POSITION_EXISTS.value
    mock_client.create_position.assert_not_called()


# ============================================================================
# 2. Happy Path Execution Handshake
# ============================================================================

def test_execute_order_happy_path_to_open(db_conn):
    mock_client = MagicMock()
    mock_client.fetch_open_positions.return_value = []
    mock_client.create_position.return_value = "deal-ref-123"

    mock_confirm = MagicMock()
    mock_confirm.status = "ACCEPTED"
    mock_confirm.deal_id = "deal-id-999"
    mock_confirm.level = 150.25
    mock_client.confirm_order.return_value = mock_confirm

    res = execute_order(
        conn=db_conn,
        broker_client=mock_client,
        client_order_id="ORD-SUCCESS",
        signal_id="sig-ord-1",
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
    assert res.order_status == OrderStatus.OPEN
    assert res.deal_reference == "deal-ref-123"
    assert res.deal_id == "deal-id-999"
    assert res.actual_fill_price == 150.25

    # Verify DB state
    row = db_conn.execute("SELECT order_status, deal_id, actual_fill_price FROM orders_and_positions WHERE client_order_id = 'ORD-SUCCESS'").fetchone()
    assert row[0] == "OPEN"
    assert row[1] == "deal-id-999"
    assert row[2] == 150.25


# ============================================================================
# 3. Fault-Injection Case A: Timeout on create_position (POST)
# ============================================================================

def test_fault_injection_create_position_timeout_preserves_submitting(db_conn):
    mock_client = MagicMock()
    mock_client.fetch_open_positions.return_value = []
    mock_client.create_position.side_effect = TimeoutError("HTTP Timeout during order dispatch")

    res = execute_order(
        conn=db_conn,
        broker_client=mock_client,
        client_order_id="ORD-TIMEOUT-POST",
        signal_id="sig-ord-1",
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
    # MUST stay SUBMITTING to allow Section 6.4 recovery to inspect broker state
    assert res.order_status == OrderStatus.SUBMITTING
    assert "TimeoutError" in (res.error_message or "")

    # DB record MUST be SUBMITTING, NEVER REJECTED!
    row = db_conn.execute("SELECT order_status, deal_reference FROM orders_and_positions WHERE client_order_id = 'ORD-TIMEOUT-POST'").fetchone()
    assert row[0] == "SUBMITTING"
    assert row[1] is None

    # Audit log must record API_TIMEOUT
    audit = db_conn.execute("SELECT event_type FROM system_audit_log WHERE client_order_id = 'ORD-TIMEOUT-POST'").fetchone()
    assert audit is not None
    assert audit[0] == AuditEventType.API_TIMEOUT.value


# ============================================================================
# 4. Fault-Injection Case B: 504 Timeout on confirm_order
# ============================================================================

def test_fault_injection_confirm_timeout_preserves_submitted(db_conn):
    mock_client = MagicMock()
    mock_client.fetch_open_positions.return_value = []
    mock_client.create_position.return_value = "deal-ref-preserve"
    mock_client.confirm_order.side_effect = TimeoutError("504 Gateway Timeout")

    res = execute_order(
        conn=db_conn,
        broker_client=mock_client,
        client_order_id="ORD-TIMEOUT-CONF",
        signal_id="sig-ord-1",
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
    # MUST stay SUBMITTED with deal_reference preserved
    assert res.order_status == OrderStatus.SUBMITTED
    assert res.deal_reference == "deal-ref-preserve"

    row = db_conn.execute("SELECT order_status, deal_reference FROM orders_and_positions WHERE client_order_id = 'ORD-TIMEOUT-CONF'").fetchone()
    assert row[0] == "SUBMITTED"
    assert row[1] == "deal-ref-preserve"

    audit = db_conn.execute("SELECT event_type FROM system_audit_log WHERE client_order_id = 'ORD-TIMEOUT-CONF'").fetchone()
    assert audit is not None
    assert audit[0] == AuditEventType.API_TIMEOUT.value


# ============================================================================
# 5. Broker Rejection on Confirm
# ============================================================================

def test_execute_order_broker_rejection(db_conn):
    mock_client = MagicMock()
    mock_client.fetch_open_positions.return_value = []
    mock_client.create_position.return_value = "deal-ref-rej"

    mock_confirm = MagicMock()
    mock_confirm.status = "REJECTED"
    mock_confirm.reject_reason = "INSUFFICIENT_MARGIN"
    mock_client.confirm_order.return_value = mock_confirm

    res = execute_order(
        conn=db_conn,
        broker_client=mock_client,
        client_order_id="ORD-REJECTED",
        signal_id="sig-ord-1",
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

    row = db_conn.execute("SELECT order_status, abort_reason FROM orders_and_positions WHERE client_order_id = 'ORD-REJECTED'").fetchone()
    assert row[0] == "REJECTED"
    assert row[1] == "BROKER_REJECTED"

    audit = db_conn.execute("SELECT event_type, details_json FROM system_audit_log WHERE client_order_id = 'ORD-REJECTED'").fetchone()
    assert audit is not None
    assert audit[0] == AuditEventType.DISCREPANCY_FOUND.value
    details = json.loads(audit[1])
    assert details["reject_reason"] == "INSUFFICIENT_MARGIN"
