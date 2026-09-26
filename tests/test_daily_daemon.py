"""
tests/test_daily_daemon.py
Unit tests verifying Master Spec V4.1 Section 6 Master Daily Daemon:
- Boot sequence recovery execution
- NYSE holiday calendar gating
- Phase 2 Pre-market checks & circuit breaker gating
- Phase 3 Market open gap, sizing, and order dispatch
- Tier 3 Reclaim monitor ticks and TTL expiry
- 15-Minute Intraday maintenance tick (reconciliation + trailing exits)
"""
import sqlite3
import pytest
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

from config.enums import (
    AbortReason,
    CloseReason,
    OrderStatus,
    SignalStatus,
    SubAccount,
    TradeDirection,
)
from scripts.run_daily_daemon import DailyDaemon, is_nyse_trading_day

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
        "VALUES ('run-daemon', '2026-09-24', 'COMPLETED', 1700000000, '{}')"
    )
    conn.execute(
        "INSERT INTO weekly_watchlist (watchlist_id, run_id, epic, sector, region, assigned_engine, "
        "permitted_direction, sunday_composite_rank, sunday_composite_score, atr_pct_60d, created_at) "
        "VALUES ('wl-daemon', 'run-daemon', 'AAPL', 'Technology', 'US', 'MEAN_REVERSION', 'LONG_ONLY', 1, 1.0, 1.5, 1700000000)"
    )
    conn.commit()
    yield conn
    conn.close()


# ============================================================================
# 1. Holiday & Boot Sequence Tests
# ============================================================================

def test_nyse_holiday_calendar_detection():
    # Good Friday 2026-04-03 -> Holiday
    assert not is_nyse_trading_day(date(2026, 4, 3))
    # Weekend Saturday 2026-09-26 -> Non-trading
    assert not is_nyse_trading_day(date(2026, 9, 26))
    # Regular Wednesday 2026-09-23 -> Trading Day
    assert is_nyse_trading_day(date(2026, 9, 23))


def test_daemon_boot_sequence_runs_recovery(db_conn):
    mock_broker = MagicMock()
    mock_broker.fetch_open_positions.return_value = []

    daemon = DailyDaemon(conn=db_conn, broker_client=mock_broker)
    report = daemon.run_boot_sequence(current_time_ms=NOW)

    assert report.phase_name == "BOOT_RECOVERY"
    assert report.success is True
    mock_broker.fetch_open_positions.assert_called_once()


# ============================================================================
# 2. Phase 2 Pre-Market Tests
# ============================================================================

def test_phase_2_promotes_signals_when_healthy(db_conn):
    db_conn.execute(
        """
        INSERT INTO daily_signals (
            signal_id, watchlist_id, epic, engine, direction, signal_date,
            signal_close_price, structural_stop_price, target_price, initial_rr_ratio,
            stop_distance_pct, provisional_priority, signal_status, reclaim_deadline_at, created_at
        ) VALUES ('sig-p2', 'wl-daemon', 'AAPL', 'MEAN_REVERSION', 'BUY', '2026-09-24', 150.0, 145.0, 160.0, 2.0, 3.33, 2.0, 'PENDING', 1700003600, 1700000000)
        """
    )
    db_conn.commit()

    daemon = DailyDaemon(conn=db_conn, broker_client=MagicMock())
    report = daemon.run_pre_market_phase(trading_date="2026-09-24", current_time_ms=NOW)

    assert report.success is True
    assert "sig-p2" in report.details["promoted_signals"]

    row = db_conn.execute("SELECT signal_status FROM daily_signals WHERE signal_id = 'sig-p2'").fetchone()
    assert row[0] == SignalStatus.PROMOTED.value


def test_phase_2_halts_when_circuit_breaker_active(db_conn):
    db_conn.execute("UPDATE system_state SET value = '1' WHERE key = 'circuit_breaker_active'")
    db_conn.commit()

    daemon = DailyDaemon(conn=db_conn, broker_client=MagicMock())
    report = daemon.run_pre_market_phase(trading_date="2026-09-24", current_time_ms=NOW)

    assert report.success is False
    assert report.details["abort_reason"] == "CIRCUIT_BREAKER_ACTIVE"


# ============================================================================
# 3. Phase 3 Market Open Tests
# ============================================================================

def test_phase_3_market_open_gap_and_dispatch(db_conn):
    db_conn.execute(
        """
        INSERT INTO daily_signals (
            signal_id, watchlist_id, epic, engine, direction, signal_date,
            signal_close_price, structural_stop_price, target_price, initial_rr_ratio,
            stop_distance_pct, provisional_priority, signal_status, reclaim_deadline_at, created_at
        ) VALUES ('sig-p3', 'wl-daemon', 'AAPL', 'MEAN_REVERSION', 'BUY', '2026-09-24', 150.0, 145.0, 160.0, 2.0, 3.33, 2.0, 'PROMOTED', 1700003600, 1700000000)
        """
    )
    db_conn.commit()

    mock_broker = MagicMock()
    mock_broker.fetch_open_positions.return_value = []
    mock_broker.create_position.return_value = "deal-ref-p3"
    confirm = MagicMock()
    confirm.status = "ACCEPTED"
    confirm.deal_id = "deal-id-p3"
    confirm.level = 150.0
    mock_broker.confirm_order.return_value = confirm

    quotes = {
        "AAPL": {
            "open": 150.0, "bid": 149.95, "ask": 150.05, "atr14": 2.0,
            "lot_step": 1.0, "min_size": 1.0, "margin_rate": 0.20
        }
    }

    daemon = DailyDaemon(conn=db_conn, broker_client=mock_broker)
    report = daemon.run_market_open_phase(
        trading_date="2026-09-24",
        live_open_quotes=quotes,
        account_equity=10_000.0,
        current_time_ms=NOW,
    )

    assert report.success is True
    assert "AAPL" in report.details["dispatched_orders"]

    order = db_conn.execute("SELECT order_status, deal_id FROM orders_and_positions WHERE signal_id = 'sig-p3'").fetchone()
    assert order is not None
    assert order[0] == "OPEN"
    assert order[1] == "deal-id-p3"


# ============================================================================
# 4. Tier 3 Reclaim Tick Tests
# ============================================================================

def test_tier_3_reclaim_tick_dispatches_on_trigger(db_conn):
    # Long setup: Signal close 150.0, Structural stop 145.0.
    # Signal is PROMOTED with active reclaim deadline
    db_conn.execute(
        """
        INSERT INTO daily_signals (
            signal_id, watchlist_id, epic, engine, direction, signal_date,
            signal_close_price, structural_stop_price, target_price, initial_rr_ratio,
            stop_distance_pct, provisional_priority, signal_status, reclaim_deadline_at, created_at
        ) VALUES ('sig-rec', 'wl-daemon', 'AAPL', 'MEAN_REVERSION', 'BUY', '2026-09-24', 150.0, 145.0, 160.0, 2.0, 3.33, 2.0, 'PROMOTED', ?, 1700000000)
        """,
        (NOW + 1800_000,),  # 30 min left
    )
    db_conn.commit()

    mock_broker = MagicMock()
    mock_broker.fetch_open_positions.return_value = []
    mock_broker.create_position.return_value = "deal-ref-rec"
    confirm = MagicMock()
    confirm.status = "ACCEPTED"
    confirm.deal_id = "deal-id-rec"
    confirm.level = 150.50
    mock_broker.confirm_order.return_value = confirm

    quotes = {
        "AAPL": {
            "current": 150.50, "high": 150.60, "low": 148.0, "bid": 150.45, "ask": 150.55,
            "lot_step": 1.0, "min_size": 1.0, "margin_rate": 0.20
        }
    }

    daemon = DailyDaemon(conn=db_conn, broker_client=mock_broker)
    reclaimed = daemon.run_reclaim_monitor_tick(
        live_quotes=quotes,
        account_equity=10_000.0,
        current_time_ms=NOW,
    )

    assert "AAPL" in reclaimed


# ============================================================================
# 5. Intraday Maintenance Tick Tests
# ============================================================================

def test_intraday_maintenance_tick_executes_target_exit(db_conn):
    db_conn.execute(
        """
        INSERT INTO orders_and_positions (
            client_order_id, signal_id, epic, sub_account, direction,
            order_status, planned_entry_price, executable_entry_price,
            actual_fill_price, initial_stop_price, current_stop_price,
            stop_version, target_price, planned_risk_r_usd, allocated_units,
            margin_used_usd, opened_at, created_at, updated_at
        ) VALUES ('ORD-TGT', NULL, 'AAPL', 'SUB_A_MR', 'BUY', 'OPEN', 100.0, 100.0, 100.0, 95.0, 95.0, 1, 104.0, 100.0, 20.0, 400.0, ?, ?, ?)
        """,
        (NOW - 600_000, NOW - 600_000, NOW - 600_000),
    )
    db_conn.commit()

    mock_broker = MagicMock()
    mock_broker.fetch_open_positions.return_value = [{
        "epic": "AAPL", "deal_id": "DEAL-1", "direction": "BUY", "size": 20.0, "stop_level": 95.0
    }]

    quotes = {
        "AAPL": {"bid": 104.20, "ask": 104.25, "current": 104.20, "high": 104.50, "low": 100.0}
    }

    daemon = DailyDaemon(conn=db_conn, broker_client=mock_broker)
    res = daemon.run_intraday_maintenance_tick(
        live_quotes=quotes,
        session_pnl_usd=0.0,
        r_unit_usd=100.0,
        current_time_ms=NOW,
    )

    assert "ORD-TGT" in res["exits_triggered"]
    mock_broker.close_position.assert_called_once_with(client_order_id="ORD-TGT")

    row = db_conn.execute("SELECT order_status, close_reason FROM orders_and_positions WHERE client_order_id = 'ORD-TGT'").fetchone()
    assert row[0] == "CLOSED"
    assert row[1] == CloseReason.TARGET_BROKER.value
