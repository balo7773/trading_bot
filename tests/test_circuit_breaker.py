"""
tests/test_circuit_breaker.py
Unit tests verifying Master Spec V4.1 Sections 5.3 and 6.6 Circuit Breaker Engine:
- Daily drawdown limit (-3R) trip & tolerance
- Rolling drawdown limit (-6R) hard trip
- Consecutive API timeouts (3x) infrastructure trip
- Soft daily reset vs hard manual reset
- Upstream order manager pre-submission gate blocking
"""
import sqlite3
import pytest
from pathlib import Path
from unittest.mock import MagicMock

from config.enums import (
    AbortReason,
    AuditEventType,
    OrderStatus,
    SubAccount,
    TradeDirection,
)
from execution.order_manager import execute_order
from monitoring.circuit_breaker import (
    audit_consecutive_timeouts,
    audit_session_drawdown,
    get_circuit_breaker_status,
    reset_circuit_breaker,
    trip_circuit_breaker,
)

NOW = 1700000000000


@pytest.fixture
def db_conn():
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON;")
    schema_path = Path("database/schema.sql")
    with open(schema_path, "r") as f:
        conn.executescript(f.read())

    # Seed parent data
    conn.execute(
        "INSERT INTO screening_runs (run_id, run_date, run_status, started_at, config_snapshot_json) "
        "VALUES ('run-cb', '2026-09-24', 'COMPLETED', 1700000000, '{}')"
    )
    conn.execute(
        "INSERT INTO weekly_watchlist (watchlist_id, run_id, epic, sector, region, assigned_engine, "
        "permitted_direction, sunday_composite_rank, sunday_composite_score, atr_pct_60d, created_at) "
        "VALUES ('wl-cb', 'run-cb', 'AAPL', 'Technology', 'US', 'MEAN_REVERSION', 'LONG_ONLY', 1, 1.0, 1.5, 1700000000)"
    )
    conn.execute(
        "INSERT INTO daily_signals (signal_id, watchlist_id, epic, engine, direction, signal_date, "
        "signal_close_price, structural_stop_price, target_price, initial_rr_ratio, stop_distance_pct, "
        "provisional_priority, signal_status, reclaim_deadline_at, created_at) "
        "VALUES ('sig-cb-1', 'wl-cb', 'AAPL', 'MEAN_REVERSION', 'BUY', '2026-09-24', 150.0, 145.0, 160.0, 2.0, 3.33, 2.0, 'PROMOTED', 1700003600, 1700000000)"
    )
    conn.commit()
    yield conn
    conn.close()


# ============================================================================
# 1. Daily & Rolling Drawdown Tests
# ============================================================================

def test_daily_drawdown_limit_trips_breaker(db_conn):
    # 1R = 100 USD. Daily loss = -310 USD (-3.1R <= -3.0R) -> Trips breaker
    tripped = audit_session_drawdown(
        conn=db_conn,
        current_time_ms=NOW,
        session_pnl_usd=-310.0,
        r_unit_usd=100.0,
    )
    assert tripped
    status = get_circuit_breaker_status(db_conn)
    assert status.is_active
    assert status.reason == "DAILY_DRAWDOWN_LIMIT"


def test_safe_daily_drawdown_does_not_trip(db_conn):
    # 1R = 100 USD. Daily loss = -150 USD (-1.5R > -3.0R) -> Does not trip
    tripped = audit_session_drawdown(
        conn=db_conn,
        current_time_ms=NOW,
        session_pnl_usd=-150.0,
        r_unit_usd=100.0,
    )
    assert not tripped
    status = get_circuit_breaker_status(db_conn)
    assert not status.is_active


def test_hard_rolling_drawdown_limit_trips(db_conn):
    # Rolling drawdown reaches -6.1R -> Trips hard master breaker
    tripped = audit_session_drawdown(
        conn=db_conn,
        current_time_ms=NOW,
        session_pnl_usd=-50.0,  # Daily loss is small
        r_unit_usd=100.0,
        rolling_drawdown_r=-6.1,
    )
    assert tripped
    status = get_circuit_breaker_status(db_conn)
    assert status.is_active
    assert status.reason == "ROLLING_DRAWDOWN_LIMIT"


# ============================================================================
# 2. Consecutive Infrastructure Timeout Tests
# ============================================================================

def test_consecutive_timeouts_trip_breaker(db_conn):
    # Log 3 consecutive API timeouts in system_audit_log
    for i in range(3):
        db_conn.execute(
            """
            INSERT INTO system_audit_log (timestamp, event_type, discrepancy_detected, details_json)
            VALUES (?, ?, 1, '{}')
            """,
            (NOW + i * 1000, AuditEventType.API_TIMEOUT.value),
        )
    db_conn.commit()

    tripped = audit_consecutive_timeouts(conn=db_conn, current_time_ms=NOW + 5000)
    assert tripped
    status = get_circuit_breaker_status(db_conn)
    assert status.is_active
    assert status.reason == "CONSECUTIVE_TIMEOUTS"


def test_interleaved_success_prevents_timeout_trip(db_conn):
    # Log 2 timeouts, 1 success, 1 timeout (not 3 consecutive)
    db_conn.execute("INSERT INTO system_audit_log (timestamp, event_type, discrepancy_detected, details_json) VALUES (?, ?, 1, '{}')", (NOW, AuditEventType.API_TIMEOUT.value))
    db_conn.execute("INSERT INTO system_audit_log (timestamp, event_type, discrepancy_detected, details_json) VALUES (?, ?, 0, '{}')", (NOW + 1000, AuditEventType.DISCREPANCY_FOUND.value))
    db_conn.execute("INSERT INTO system_audit_log (timestamp, event_type, discrepancy_detected, details_json) VALUES (?, ?, 1, '{}')", (NOW + 2000, AuditEventType.API_TIMEOUT.value))
    db_conn.commit()

    tripped = audit_consecutive_timeouts(conn=db_conn, current_time_ms=NOW + 5000)
    assert not tripped
    status = get_circuit_breaker_status(db_conn)
    assert not status.is_active


# ============================================================================
# 3. Soft Daily Reset vs Hard Manual Reset Tests
# ============================================================================

def test_soft_daily_breaker_resets_normally(db_conn):
    trip_circuit_breaker(db_conn, reason="DAILY_DRAWDOWN_LIMIT", current_time_ms=NOW)
    assert get_circuit_breaker_status(db_conn).is_active

    # Daily reset at 00:00 without force=True succeeds
    success = reset_circuit_breaker(conn=db_conn, current_time_ms=NOW + 1000, force=False)
    assert success
    assert not get_circuit_breaker_status(db_conn).is_active


def test_hard_rolling_breaker_requires_forced_reset(db_conn):
    trip_circuit_breaker(db_conn, reason="ROLLING_DRAWDOWN_LIMIT", current_time_ms=NOW)

    # Attempting normal reset without force=True fails
    success = reset_circuit_breaker(conn=db_conn, current_time_ms=NOW + 1000, force=False)
    assert not success
    assert get_circuit_breaker_status(db_conn).is_active  # Still locked!

    # Forced manual operator reset succeeds
    success_force = reset_circuit_breaker(conn=db_conn, current_time_ms=NOW + 2000, force=True)
    assert success_force
    assert not get_circuit_breaker_status(db_conn).is_active


# ============================================================================
# 4. Integration Test: Order Manager Pre-Submission Gate
# ============================================================================

def test_tripped_circuit_breaker_blocks_order_manager(db_conn):
    trip_circuit_breaker(db_conn, reason="DAILY_DRAWDOWN_LIMIT", current_time_ms=NOW)

    mock_client = MagicMock()
    mock_client.fetch_open_positions.return_value = []

    res = execute_order(
        conn=db_conn,
        broker_client=mock_client,
        client_order_id="ORD-CB-BLOCKED",
        signal_id="sig-cb-1",
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
