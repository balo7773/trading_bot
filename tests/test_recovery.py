"""
tests/test_recovery.py
Unit tests verifying Master Spec V4.1 Section 6.4 Startup Recovery Matrix:
- SUBMITTED resolution via confirm_order (ACCEPTED/REJECTED)
- SUBMITTED fallback adoption via /positions
- SUBMITTING composite match adoption
- SUBMITTING expired timeout abortion
- QUEUED safe cancellation
"""
import sqlite3
import pytest
from pathlib import Path
from unittest.mock import MagicMock

from config.enums import AbortReason, OrderStatus, TradeDirection
from monitoring.recovery import run_startup_recovery

NOW = 1700000000000


@pytest.fixture
def db_conn():
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON;")
    schema_path = Path("database/schema.sql")
    with open(schema_path, "r") as f:
        conn.executescript(f.read())

    # Seed Parent Records
    conn.execute(
        "INSERT INTO screening_runs (run_id, run_date, run_status, started_at, config_snapshot_json) "
        "VALUES ('run-recov', '2026-09-24', 'COMPLETED', 1700000000, '{}')"
    )
    conn.execute(
        "INSERT INTO weekly_watchlist (watchlist_id, run_id, epic, sector, region, assigned_engine, "
        "permitted_direction, sunday_composite_rank, sunday_composite_score, atr_pct_60d, created_at) "
        "VALUES ('wl-recov', 'run-recov', 'AAPL', 'Technology', 'US', 'MEAN_REVERSION', 'LONG_ONLY', 1, 1.0, 1.5, 1700000000)"
    )
    conn.execute(
        "INSERT INTO daily_signals (signal_id, watchlist_id, epic, engine, direction, signal_date, "
        "signal_close_price, structural_stop_price, target_price, initial_rr_ratio, stop_distance_pct, "
        "provisional_priority, signal_status, reclaim_deadline_at, created_at) "
        "VALUES ('sig-recov-1', 'wl-recov', 'AAPL', 'MEAN_REVERSION', 'BUY', '2026-09-24', 150.0, 145.0, 160.0, 2.0, 3.33, 2.0, 'PROMOTED', 1700003600, 1700000000)"
    )
    conn.commit()
    yield conn
    conn.close()


def insert_order_state(
    conn,
    order_id: str,
    status: str,
    deal_ref: str = None,
    units: float = 20.0,
    updated_at: int = NOW - 1200_000,  # 20 mins ago (older than 10-min grace)
):
    conn.execute(
        """
        INSERT INTO orders_and_positions (
            client_order_id, signal_id, epic, sub_account, direction,
            order_status, planned_entry_price, executable_entry_price,
            initial_stop_price, current_stop_price, target_price,
            planned_risk_r_usd, allocated_units, margin_used_usd,
            deal_reference, created_at, updated_at
        ) VALUES (?, 'sig-recov-1', 'AAPL', 'SUB_A_MR', 'BUY', ?, 150.0, 150.0, 145.0, 145.0, 160.0, 100.0, ?, 600.0, ?, ?, ?)
        """,
        (order_id, status, units, deal_ref, updated_at, updated_at),
    )
    conn.commit()


# ============================================================================
# 1. SUBMITTED Resolution Tests
# ============================================================================

def test_recovery_submitted_confirm_accepted(db_conn):
    insert_order_state(db_conn, order_id="ORD-SUB-ACC", status="SUBMITTED", deal_ref="ref-acc")
    mock_client = MagicMock()
    mock_client.fetch_open_positions.return_value = []
    
    confirm = MagicMock()
    confirm.status = "ACCEPTED"
    confirm.deal_id = "deal-acc-1"
    confirm.level = 150.25
    mock_client.confirm_order.return_value = confirm

    report = run_startup_recovery(conn=db_conn, broker_client=mock_client, current_time_ms=NOW)
    assert "ORD-SUB-ACC" in report.submitted_resolved_open

    row = db_conn.execute("SELECT order_status, deal_id, actual_fill_price, trailing_high_watermark FROM orders_and_positions WHERE client_order_id = 'ORD-SUB-ACC'").fetchone()
    assert row[0] == "OPEN"
    assert row[1] == "deal-acc-1"
    assert row[2] == 150.25
    assert row[3] == 150.25  # Watermark initialized


def test_recovery_submitted_confirm_rejected(db_conn):
    insert_order_state(db_conn, order_id="ORD-SUB-REJ", status="SUBMITTED", deal_ref="ref-rej")
    mock_client = MagicMock()
    mock_client.fetch_open_positions.return_value = []

    confirm = MagicMock()
    confirm.status = "REJECTED"
    confirm.reject_reason = "MARKET_CLOSED"
    mock_client.confirm_order.return_value = confirm

    report = run_startup_recovery(conn=db_conn, broker_client=mock_client, current_time_ms=NOW)
    assert "ORD-SUB-REJ" in report.submitted_resolved_rejected

    row = db_conn.execute("SELECT order_status, abort_reason FROM orders_and_positions WHERE client_order_id = 'ORD-SUB-REJ'").fetchone()
    assert row[0] == "REJECTED"
    assert row[1] == AbortReason.BROKER_REJECTED.value


def test_recovery_submitted_fallback_to_positions(db_conn):
    # Confirm endpoint 504s/404s, but /positions has the open position
    insert_order_state(db_conn, order_id="ORD-SUB-FALLBACK", status="SUBMITTED", deal_ref="ref-dead")
    mock_client = MagicMock()
    mock_client.confirm_order.side_effect = TimeoutError("504 Gateway Timeout")
    mock_client.fetch_open_positions.return_value = [{
        "epic": "AAPL", "deal_id": "DEAL-FALLBACK", "direction": "BUY", "size": 20.0, "level": 150.10
    }]

    report = run_startup_recovery(conn=db_conn, broker_client=mock_client, current_time_ms=NOW)
    assert "ORD-SUB-FALLBACK" in report.submitted_resolved_open

    row = db_conn.execute("SELECT order_status, deal_id, actual_fill_price FROM orders_and_positions WHERE client_order_id = 'ORD-SUB-FALLBACK'").fetchone()
    assert row[0] == "OPEN"
    assert row[1] == "DEAL-FALLBACK"


# ============================================================================
# 2. SUBMITTING Resolution Tests
# ============================================================================

def test_recovery_submitting_composite_match_adopts(db_conn):
    # Network dropped on POST, no deal_reference exists
    insert_order_state(db_conn, order_id="ORD-SUBMITTING-MATCH", status="SUBMITTING", units=20.0)
    mock_client = MagicMock()
    mock_client.fetch_open_positions.return_value = [{
        "epic": "AAPL", "deal_id": "DEAL-ORPHAN-RECOV", "direction": "BUY", "size": 20.0, "level": 150.30
    }]

    report = run_startup_recovery(conn=db_conn, broker_client=mock_client, current_time_ms=NOW)
    assert "ORD-SUBMITTING-MATCH" in report.submitting_adopted_open

    row = db_conn.execute("SELECT order_status, deal_id, actual_fill_price FROM orders_and_positions WHERE client_order_id = 'ORD-SUBMITTING-MATCH'").fetchone()
    assert row[0] == "OPEN"
    assert row[1] == "DEAL-ORPHAN-RECOV"


def test_recovery_submitting_expired_aborts(db_conn):
    # No matching broker position found, older than 10-min grace -> Safe to abort
    insert_order_state(db_conn, order_id="ORD-SUBMITTING-EXPIRED", status="SUBMITTING", updated_at=NOW - 1200_000)
    mock_client = MagicMock()
    mock_client.fetch_open_positions.return_value = []

    report = run_startup_recovery(conn=db_conn, broker_client=mock_client, current_time_ms=NOW)
    assert "ORD-SUBMITTING-EXPIRED" in report.submitting_abandoned_aborted

    row = db_conn.execute("SELECT order_status, abort_reason FROM orders_and_positions WHERE client_order_id = 'ORD-SUBMITTING-EXPIRED'").fetchone()
    assert row[0] == "ABORTED"
    assert row[1] == AbortReason.TIMEOUT.value


# ============================================================================
# 3. QUEUED Cancellation Tests
# ============================================================================

def test_recovery_queued_clean_cancellation(db_conn):
    insert_order_state(db_conn, order_id="ORD-QUEUED-CLEAN", status="QUEUED")
    mock_client = MagicMock()
    mock_client.fetch_open_positions.return_value = []

    report = run_startup_recovery(conn=db_conn, broker_client=mock_client, current_time_ms=NOW)
    assert "ORD-QUEUED-CLEAN" in report.queued_cancelled

    row = db_conn.execute("SELECT order_status, abort_reason FROM orders_and_positions WHERE client_order_id = 'ORD-QUEUED-CLEAN'").fetchone()
    assert row[0] == "ABORTED"
    assert row[1] == AbortReason.TIMEOUT.value
