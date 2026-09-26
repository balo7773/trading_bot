"""
tests/test_strategies.py
Unit tests verifying strategy engines, deterministic IDs, gating logic,
and SQLite schema constraints.
"""
import sqlite3
import numpy as np
import pytest
from pathlib import Path

from config.enums import AbortReason, EngineType, PermittedDirection, SignalStatus, TradeDirection
from config.settings import ENGINE_A_RR_FLOOR
from strategies.base import DailySignalCandidate
from strategies.engine_a_mr import evaluate_engine_a
from strategies.engine_b_bo import evaluate_engine_b


# ============================================================================
# 1. Deterministic Identity & Data Model Tests
# ============================================================================

def test_daily_signal_candidate_deterministic_id():
    """Confirms signal_id is computed deterministically from date, epic, engine."""
    cand = DailySignalCandidate(
        watchlist_id="wl-1",
        epic="AAPL",
        engine=EngineType.MEAN_REVERSION,
        direction=TradeDirection.BUY,
        signal_date="2026-09-24",
        signal_close_price=150.0,
        structural_stop_price=145.0,
        stop_distance_pct=3.33,
        provisional_priority=1.5,
        reclaim_deadline_at=1700003600,
        created_at=1700000000,
        target_price=157.5,
        initial_rr_ratio=1.5,
    )
    assert cand.signal_id == "SIG_20260924_AAPL_MEAN_REVERSION"


# ============================================================================
# 2. Engine A (Mean Reversion) Tests
# ============================================================================

def make_engine_a_series():
    """Generates 250 bars with a rising 200 SMA and a valid BB dip & reclaim."""
    n = 250
    close = np.linspace(100.0, 160.0, n, dtype=np.float64)
    close[-30:] = 155.0
    high = close + 2.0
    low = close - 2.0

    # T-2: baseline
    close[-3], high[-3], low[-3] = 155.0, 157.0, 153.0
    # T-1: Pierces lower band (~151.0)
    close[-2], high[-2], low[-2] = 149.0, 152.0, 148.0
    # T: Reclaims lower band
    close[-1], high[-1], low[-1] = 152.0, 154.0, 150.0

    return high, low, close


def test_engine_a_setup_fires_and_populates_fields():
    """Confirms Engine A detects the setup and populates all required fields."""
    high, low, close = make_engine_a_series()
    sig = evaluate_engine_a(
        watchlist_id="wl-001",
        epic="AAPL",
        signal_date="2026-09-24",
        high_p=high,
        low_p=low,
        close_p=close,
        permitted_direction=PermittedDirection.LONG_ONLY,
    )
    assert sig is not None
    assert sig.engine == EngineType.MEAN_REVERSION
    assert sig.direction == TradeDirection.BUY
    assert sig.target_price is not None
    assert sig.initial_rr_ratio is not None
    assert sig.structural_stop_price == 148.0  # min(153, 148, 150)
    assert sig.breakout_thrust_atr is None  # Must be None for Engine A


def test_engine_a_rr_floor_abort_logic():
    """Unit test: verifies R:R floor gate cleanly marks signal as ABORTED."""
    high, low, close = make_engine_a_series()
    
    # Passing an unattainable floor forces ABORTED state deterministically
    sig = evaluate_engine_a(
        watchlist_id="wl-001",
        epic="AAPL",
        signal_date="2026-09-24",
        high_p=high,
        low_p=low,
        close_p=close,
        permitted_direction=PermittedDirection.LONG_ONLY,
        rr_floor=99.0,
    )
    assert sig is not None
    assert sig.signal_status == SignalStatus.ABORTED
    assert sig.abort_reason == AbortReason.RR_BELOW_FLOOR
    # chk_engine_signal_integrity: aborted MR signals must STILL carry target and ratio
    assert sig.target_price is not None
    assert sig.initial_rr_ratio is not None


# ============================================================================
# 3. Engine B (Breakout) Tests
# ============================================================================

def test_engine_b_long_breakout_approved():
    n = 60
    close = np.full(n, 100.0, dtype=np.float64)
    high = close + 1.0
    low = close - 1.0

    # Bar T breaks above 20-day high (101.0)
    close[-1], high[-1], low[-1] = 103.0, 104.0, 102.0

    sig = evaluate_engine_b(
        watchlist_id="wl-002",
        epic="NVDA",
        signal_date="2026-09-24",
        high_p=high,
        low_p=low,
        close_p=close,
        compression_threshold=0.010,
    )
    assert sig is not None
    assert sig.engine == EngineType.BREAKOUT
    assert sig.direction == TradeDirection.BUY
    assert sig.signal_status == SignalStatus.PENDING
    assert sig.target_price is None
    assert sig.initial_rr_ratio is None
    assert sig.breakout_thrust_atr > 0.0
    assert sig.compression_spread_pct < 1.0


def test_engine_b_compression_gate_rejection():
    n = 60
    close = np.full(n, 80.0, dtype=np.float64)
    close[-20:] = 120.0  # Creates wide divergence between EMA32 and SMA20
    high = close + 1.0
    low = close - 1.0

    close[-1], high[-1] = 125.0, 126.0

    sig = evaluate_engine_b(
        watchlist_id="wl-002",
        epic="NVDA",
        signal_date="2026-09-24",
        high_p=high,
        low_p=low,
        close_p=close,
        compression_threshold=0.010,
    )
    assert sig is None


# ============================================================================
# 4. Database Schema Parity Tests
# ============================================================================

def test_database_signal_schema_integrity():
    """
    Verifies that candidates strictly satisfy daily_signals CHECK constraints:
    - Engine A must have target_price and initial_rr_ratio (even if ABORTED)
    - Engine B must have NULL target_price and NULL initial_rr_ratio
    - Aborted status must carry abort_reason
    """
    schema_path = Path("database/schema.sql")
    if not schema_path.exists():
        pytest.skip("database/schema.sql not found.")

    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON;")
    with open(schema_path, "r") as f:
        conn.executescript(f.read())

    # Seed Foreign Key parents
    conn.execute(
        "INSERT INTO screening_runs (run_id, run_date, run_status, started_at, config_snapshot_json) "
        "VALUES ('run-test', '2026-09-24', 'COMPLETED', 1700000000, '{}')"
    )
    conn.execute(
        "INSERT INTO weekly_watchlist (watchlist_id, run_id, epic, sector, region, assigned_engine, "
        "permitted_direction, sunday_composite_rank, sunday_composite_score, atr_pct_60d, created_at) "
        "VALUES ('wl-test', 'run-test', 'AAPL', 'Technology', 'US', 'MEAN_REVERSION', 'LONG_ONLY', 1, 1.0, 1.5, 1700000000)"
    )

    insert_sql = """
        INSERT INTO daily_signals (
            signal_id, watchlist_id, epic, engine, direction, signal_date,
            signal_close_price, structural_stop_price, target_price, initial_rr_ratio,
            stop_distance_pct, provisional_priority, signal_status, abort_reason,
            reclaim_deadline_at, created_at, breakout_thrust_atr, compression_spread_pct
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """

    # Test 1: Insert valid Mean Reversion signal (ABORTED state keeps target and ratio)
    sig_mr = DailySignalCandidate(
        watchlist_id="wl-test",
        epic="AAPL",
        engine=EngineType.MEAN_REVERSION,
        direction=TradeDirection.BUY,
        signal_date="2026-09-24",
        signal_close_price=150.0,
        structural_stop_price=145.0,
        stop_distance_pct=3.33,
        provisional_priority=0.8,
        reclaim_deadline_at=1700003600,
        created_at=1700000000,
        signal_status=SignalStatus.ABORTED,
        abort_reason=AbortReason.RR_BELOW_FLOOR,
        target_price=154.0,
        initial_rr_ratio=0.8,
    )
    conn.execute(insert_sql, (
        sig_mr.signal_id, sig_mr.watchlist_id, sig_mr.epic, sig_mr.engine.value, sig_mr.direction.value,
        sig_mr.signal_date, sig_mr.signal_close_price, sig_mr.structural_stop_price, sig_mr.target_price,
        sig_mr.initial_rr_ratio, sig_mr.stop_distance_pct, sig_mr.provisional_priority, sig_mr.signal_status.value,
        sig_mr.abort_reason.value, sig_mr.reclaim_deadline_at, sig_mr.created_at,
        sig_mr.breakout_thrust_atr, sig_mr.compression_spread_pct
    ))
    conn.commit()

    # Test 2: Negative check - Engine B with populated target_price must fail CHECK constraint
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(insert_sql, (
            "SIG_FAIL_BO", "wl-test", "AAPL", "BREAKOUT", "BUY",
            "2026-09-24", 100.0, 95.0, 110.0,  # Invalid: target_price must be NULL
            None, 5.0, 1.0, "PENDING", None, 1700003600, 1700000000, 1.5, 0.5
        ))
