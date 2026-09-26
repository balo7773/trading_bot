"""
tests/test_reconciliation.py
Unit tests verifying Master Spec V4.1 Section 6.5 Position & Stop Reconciliation:
- Ghost exit detection & grace period
- Direction-aware stop repair (looser) & stop adoption (tighter)
- Emergency no-stop injection
- Partial close synchronization
- Late-fill orphan adoption
- Foreign unowned position containment (epic_blocks)
"""
import json
import sqlite3
import pytest
from pathlib import Path
from unittest.mock import MagicMock

from config.enums import (
    AuditEventType,
    BlockReason,
    CloseReason,
    OrderStatus,
    TradeDirection,
)
from monitoring.reconciliation import reconcile_positions

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
        "VALUES ('run-rec', '2026-09-24', 'COMPLETED', 1700000000, '{}')"
    )
    conn.execute(
        "INSERT INTO weekly_watchlist (watchlist_id, run_id, epic, sector, region, assigned_engine, "
        "permitted_direction, sunday_composite_rank, sunday_composite_score, atr_pct_60d, created_at) "
        "VALUES ('wl-rec', 'run-rec', 'AAPL', 'Technology', 'US', 'MEAN_REVERSION', 'LONG_ONLY', 1, 1.0, 1.5, 1700000000)"
    )
    conn.execute(
        "INSERT INTO daily_signals (signal_id, watchlist_id, epic, engine, direction, signal_date, "
        "signal_close_price, structural_stop_price, target_price, initial_rr_ratio, stop_distance_pct, "
        "provisional_priority, signal_status, reclaim_deadline_at, created_at) "
        "VALUES ('sig-rec-1', 'wl-rec', 'AAPL', 'MEAN_REVERSION', 'BUY', '2026-09-24', 150.0, 145.0, 160.0, 2.0, 3.33, 2.0, 'PROMOTED', 1700003600, 1700000000)"
    )
    conn.commit()
    yield conn
    conn.close()


def insert_order(
    conn,
    order_id="ORD-1",
    epic="AAPL",
    direction="BUY",
    status="OPEN",
    units=20.0,
    fill=150.0,
    stop=145.0,
    stop_ver=1,
    margin=600.0,
    opened_at=NOW - 600_000,  # 10 mins ago
    updated_at=NOW - 60_000,   # 1 min ago
):
    conn.execute(
        """
        INSERT INTO orders_and_positions (
            client_order_id, signal_id, epic, sub_account, direction,
            order_status, planned_entry_price, executable_entry_price,
            actual_fill_price, initial_stop_price, current_stop_price,
            stop_version, target_price, planned_risk_r_usd, allocated_units,
            margin_used_usd, opened_at, created_at, updated_at
        ) VALUES (?, 'sig-rec-1', ?, 'SUB_A_MR', ?, ?, 150.0, 150.0, ?, ?, ?, ?, 160.0, 100.0, ?, ?, ?, ?, ?)
        """,
        (order_id, epic, direction, status, fill, stop, stop, stop_ver, units, margin, opened_at, opened_at, updated_at),
    )
    conn.commit()


# ============================================================================
# 1. Ghost Exit & Grace Period Tests
# ============================================================================

def test_ghost_exit_detected_and_closed(db_conn):
    insert_order(db_conn, order_id="ORD-GHOST", opened_at=NOW - 600_000, updated_at=NOW - 60_000)
    mock_client = MagicMock()
    mock_client.fetch_open_positions.return_value = []  # No positions on broker

    report = reconcile_positions(conn=db_conn, broker_client=mock_client, current_time_ms=NOW)
    assert "ORD-GHOST" in report.ghost_exits_closed

    row = db_conn.execute("SELECT order_status, close_reason FROM orders_and_positions WHERE client_order_id = 'ORD-GHOST'").fetchone()
    assert row[0] == "CLOSED"
    assert row[1] == CloseReason.STOP_BROKER.value


def test_ghost_exit_grace_period_skipped(db_conn):
    # Opened only 2 minutes ago (< 5 minute grace window)
    insert_order(db_conn, order_id="ORD-GRACE", opened_at=NOW - 120_000, updated_at=NOW - 120_000)
    mock_client = MagicMock()
    mock_client.fetch_open_positions.return_value = []

    report = reconcile_positions(conn=db_conn, broker_client=mock_client, current_time_ms=NOW)
    assert "ORD-GRACE" not in report.ghost_exits_closed

    row = db_conn.execute("SELECT order_status FROM orders_and_positions WHERE client_order_id = 'ORD-GRACE'").fetchone()
    assert row[0] == "OPEN"


# ============================================================================
# 2. Stop Level Audits: Repair (Looser) vs Adopt (Tighter) vs Emergency Injection
# ============================================================================

def test_stop_repair_long_broker_looser(db_conn):
    # Long: Local stop = 145.0. Broker stop = 142.0 (looser -> expands risk)
    insert_order(db_conn, order_id="ORD-LONG-LOOSE", stop=145.0)
    mock_client = MagicMock()
    mock_client.fetch_open_positions.return_value = [{
        "epic": "AAPL", "deal_id": "DEAL-1", "direction": "BUY", "size": 20.0, "stop_level": 142.0
    }]

    report = reconcile_positions(conn=db_conn, broker_client=mock_client, current_time_ms=NOW)
    assert "ORD-LONG-LOOSE" in report.stops_repaired
    mock_client.update_position.assert_called_once_with(deal_id="DEAL-1", stop_level=145.0)


def test_stop_adopt_long_broker_tighter(db_conn):
    # Long: Local stop = 145.0. Broker stop = 148.0 (tighter -> protects profit)
    insert_order(db_conn, order_id="ORD-LONG-TIGHT", stop=145.0, stop_ver=1)
    mock_client = MagicMock()
    mock_client.fetch_open_positions.return_value = [{
        "epic": "AAPL", "deal_id": "DEAL-1", "direction": "BUY", "size": 20.0, "stop_level": 148.0
    }]

    report = reconcile_positions(conn=db_conn, broker_client=mock_client, current_time_ms=NOW)
    assert "ORD-LONG-TIGHT" in report.stops_adopted
    mock_client.update_position.assert_not_called()

    row = db_conn.execute("SELECT current_stop_price, stop_version FROM orders_and_positions WHERE client_order_id = 'ORD-LONG-TIGHT'").fetchone()
    assert row[0] == 148.0
    assert row[1] == 2  # Version incremented


def test_stop_repair_short_broker_looser(db_conn):
    # Short: Local stop = 105.0. Broker stop = 108.0 (looser -> expands risk)
    insert_order(db_conn, order_id="ORD-SHORT-LOOSE", direction="SELL_SHORT", stop=105.0)
    mock_client = MagicMock()
    mock_client.fetch_open_positions.return_value = [{
        "epic": "AAPL", "deal_id": "DEAL-2", "direction": "SELL", "size": 20.0, "stop_level": 108.0
    }]

    report = reconcile_positions(conn=db_conn, broker_client=mock_client, current_time_ms=NOW)
    assert "ORD-SHORT-LOOSE" in report.stops_repaired
    mock_client.update_position.assert_called_once_with(deal_id="DEAL-2", stop_level=105.0)


def test_stop_adopt_short_broker_tighter(db_conn):
    # Short: Local stop = 105.0. Broker stop = 102.0 (tighter -> protects profit)
    insert_order(db_conn, order_id="ORD-SHORT-TIGHT", direction="SELL_SHORT", stop=105.0, stop_ver=1)
    mock_client = MagicMock()
    mock_client.fetch_open_positions.return_value = [{
        "epic": "AAPL", "deal_id": "DEAL-2", "direction": "SELL", "size": 20.0, "stop_level": 102.0
    }]

    report = reconcile_positions(conn=db_conn, broker_client=mock_client, current_time_ms=NOW)
    assert "ORD-SHORT-TIGHT" in report.stops_adopted
    row = db_conn.execute("SELECT current_stop_price, stop_version FROM orders_and_positions WHERE client_order_id = 'ORD-SHORT-TIGHT'").fetchone()
    assert row[0] == 102.0
    assert row[1] == 2


def test_emergency_no_stop_injection(db_conn):
    # Broker position has NO stop (stop_level is None)
    insert_order(db_conn, order_id="ORD-NO-STOP", stop=145.0)
    mock_client = MagicMock()
    mock_client.fetch_open_positions.return_value = [{
        "epic": "AAPL", "deal_id": "DEAL-EMERG", "direction": "BUY", "size": 20.0, "stop_level": None
    }]

    report = reconcile_positions(conn=db_conn, broker_client=mock_client, current_time_ms=NOW)
    assert "ORD-NO-STOP" in report.emergency_stops_injected
    mock_client.update_position.assert_called_once_with(deal_id="DEAL-EMERG", stop_level=145.0)


def test_stop_repair_in_flight_mutation_cooldown_skipped(db_conn):
    # Order was updated 10 seconds ago (< 30s cooldown): Skip stop audit
    insert_order(db_conn, order_id="ORD-COOLDOWN", stop=145.0, updated_at=NOW - 10_000)
    mock_client = MagicMock()
    mock_client.fetch_open_positions.return_value = [{
        "epic": "AAPL", "deal_id": "DEAL-1", "direction": "BUY", "size": 20.0, "stop_level": 140.0
    }]

    report = reconcile_positions(conn=db_conn, broker_client=mock_client, current_time_ms=NOW)
    assert "ORD-COOLDOWN" not in report.stops_repaired
    mock_client.update_position.assert_not_called()


# ============================================================================
# 3. Partial Closes, Late-Fill Adoption & Foreign Containment
# ============================================================================

def test_partial_close_adjusts_units_and_margin(db_conn):
    # Local units = 20, margin = 600. Broker size = 10 (half closed)
    insert_order(db_conn, order_id="ORD-PARTIAL", units=20.0, margin=600.0)
    mock_client = MagicMock()
    mock_client.fetch_open_positions.return_value = [{
        "epic": "AAPL", "deal_id": "DEAL-1", "direction": "BUY", "size": 10.0, "stop_level": 145.0
    }]

    report = reconcile_positions(conn=db_conn, broker_client=mock_client, current_time_ms=NOW)
    assert "ORD-PARTIAL" in report.partial_closes_synced

    row = db_conn.execute("SELECT allocated_units, margin_used_usd FROM orders_and_positions WHERE client_order_id = 'ORD-PARTIAL'").fetchone()
    assert row[0] == 10.0
    assert row[1] == 300.0  # Proportionally halved


def test_orphan_late_fill_adoption(db_conn):
    # Order in SUBMITTING (Case A timeout)
    insert_order(db_conn, order_id="ORD-ORPHAN", status="SUBMITTING", units=20.0, stop=145.0)
    mock_client = MagicMock()
    mock_client.fetch_open_positions.return_value = [{
        "epic": "AAPL", "deal_id": "DEAL-ADOPT", "direction": "BUY", "size": 20.0, "level": 150.50, "stop_level": 145.0
    }]

    report = reconcile_positions(conn=db_conn, broker_client=mock_client, current_time_ms=NOW)
    assert "ORD-ORPHAN" in report.orphans_adopted

    row = db_conn.execute("SELECT order_status, deal_id, actual_fill_price, trailing_high_watermark FROM orders_and_positions WHERE client_order_id = 'ORD-ORPHAN'").fetchone()
    assert row[0] == "OPEN"
    assert row[1] == "DEAL-ADOPT"
    assert row[2] == 150.50
    assert row[3] == 150.50  # Watermark initialized


def test_foreign_unowned_position_blocks_epic(db_conn):
    # Broker shows position on TSLA, which is not tracked in local DB
    mock_client = MagicMock()
    mock_client.fetch_open_positions.return_value = [{
        "epic": "TSLA", "deal_id": "DEAL-FOREIGN", "direction": "BUY", "size": 5.0, "stop_level": 200.0
    }]

    report = reconcile_positions(conn=db_conn, broker_client=mock_client, current_time_ms=NOW)
    assert "TSLA" in report.blocked_foreign_epics

    block = db_conn.execute("SELECT reason FROM epic_blocks WHERE epic = 'TSLA' AND unblocked_at IS NULL").fetchone()
    assert block is not None
    assert block[0] == BlockReason.MANUAL_POSITION_EXISTS.value
