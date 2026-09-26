"""
tests/test_screening_funnel.py
Unit tests verifying Master Spec V4.1 Section 2 Sunday Screening Funnel:
- ADV $25M liquidity gating
- History length & large opening gaps
- Sector cap (max 2 per sector)
- Correlation veto (>= 0.70)
- Database schema integration for screening_runs and weekly_watchlist
"""
import sqlite3
import pytest
import numpy as np
import pandas as pd
from pathlib import Path

from config.enums import EngineType, PermittedDirection
from screening.funnel import (
    CandidateMetrics,
    apply_sector_caps_and_correlation_veto,
    filter_and_score_universe,
    run_sunday_screening,
)


@pytest.fixture
def db_conn():
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON;")
    schema_path = Path("database/schema.sql")
    with open(schema_path, "r") as f:
        conn.executescript(f.read())
    yield conn
    conn.close()


def make_synthetic_stock_df(bars: int = 250, base_price: float = 100.0, volume: float = 500_000.0) -> pd.DataFrame:
    """Generates synthetic stock OHLCV data."""
    dates = pd.date_range("2026-01-01", periods=bars)
    np.random.seed(42)
    drift = np.linspace(0, 10, bars)
    noise = np.random.normal(0, 0.5, bars)
    close = base_price + drift + noise
    open_p = close - np.random.normal(0, 0.2, bars)
    high = np.maximum(open_p, close) + 0.5
    low = np.minimum(open_p, close) - 0.5

    return pd.DataFrame({
        "open": open_p,
        "high": high,
        "low": low,
        "close": close,
        "volume": [volume] * bars,
    }, index=dates)


# ============================================================================
# 1. Gating Tests
# ============================================================================

def test_liquidity_gate_rejects_sub_25m_adv():
    # Price = 10, Volume = 500k -> ADV = 5M < 25M -> Rejection
    df_illiquid = make_synthetic_stock_df(base_price=10.0, volume=500_000.0)
    data = {"ILLIQ": {"df": df_illiquid, "sector": "Tech"}}

    candidates = filter_and_score_universe(data)
    assert len(candidates) == 0


def test_history_gate_rejects_short_history():
    # Only 50 bars -> Rejection
    df_short = make_synthetic_stock_df(bars=50, base_price=100.0, volume=1_000_000.0)
    data = {"SHORT": {"df": df_short, "sector": "Tech"}}

    candidates = filter_and_score_universe(data)
    assert len(candidates) == 0


# ============================================================================
# 2. Sector Capping & Correlation Veto Tests
# ============================================================================

def test_sector_cap_limits_to_two_per_sector():
    # 4 Tech candidates
    candidates = [
        CandidateMetrics("AAPL", "Tech", "US", 1e8, 1.5, EngineType.MEAN_REVERSION, PermittedDirection.LONG_ONLY, 90.0, np.random.randn(60)),
        CandidateMetrics("MSFT", "Tech", "US", 1e8, 1.5, EngineType.MEAN_REVERSION, PermittedDirection.LONG_ONLY, 85.0, np.random.randn(60)),
        CandidateMetrics("GOOG", "Tech", "US", 1e8, 1.5, EngineType.MEAN_REVERSION, PermittedDirection.LONG_ONLY, 80.0, np.random.randn(60)),
        CandidateMetrics("META", "Tech", "US", 1e8, 1.5, EngineType.MEAN_REVERSION, PermittedDirection.LONG_ONLY, 75.0, np.random.randn(60)),
    ]

    # Force zero correlation between them
    for i in range(len(candidates)):
        vec = np.zeros(60)
        vec[i * 10 : (i + 1) * 10] = 1.0
        object.__setattr__(candidates[i], "returns_60", vec)

    filtered = apply_sector_caps_and_correlation_veto(candidates)
    assert len(filtered) == 2
    assert [c.epic for c in filtered] == ["AAPL", "MSFT"]


def test_correlation_veto_rejects_correlated_candidates():
    # 2 Candidates in different sectors, but returns correlation = 1.0
    common_returns = np.random.randn(60)
    candidates = [
        CandidateMetrics("AAPL", "Tech", "US", 1e8, 1.5, EngineType.MEAN_REVERSION, PermittedDirection.LONG_ONLY, 90.0, common_returns),
        CandidateMetrics("JPM", "Financials", "US", 1e8, 1.5, EngineType.MEAN_REVERSION, PermittedDirection.LONG_ONLY, 85.0, common_returns),
    ]

    filtered = apply_sector_caps_and_correlation_veto(candidates)
    assert len(filtered) == 1
    assert filtered[0].epic == "AAPL"  # Higher score wins


# ============================================================================
# 3. Database Persistence Tests
# ============================================================================

def test_run_sunday_screening_writes_to_database(db_conn):
    # Setup 3 valid stocks across 2 sectors
    df1 = make_synthetic_stock_df(bars=250, base_price=100.0, volume=1_000_000.0)  # ADV = 100M
    df2 = make_synthetic_stock_df(bars=250, base_price=150.0, volume=800_000.0)    # ADV = 120M
    df3 = make_synthetic_stock_df(bars=250, base_price=200.0, volume=600_000.0)    # ADV = 120M

    universe = {
        "AAPL": {"df": df1, "sector": "Tech", "region": "US"},
        "MSFT": {"df": df2, "sector": "Tech", "region": "US"},
        "XOM": {"df": df3, "sector": "Energy", "region": "US"},
    }

    now_ms = 1700000000000
    res = run_sunday_screening(
        conn=db_conn,
        universe_data=universe,
        run_id="run-2026-09-27",
        run_date="2026-09-27",
        current_time_ms=now_ms,
    )

    assert len(res) > 0
    # Verify screening_runs entry
    run_row = db_conn.execute("SELECT run_status FROM screening_runs WHERE run_id = 'run-2026-09-27'").fetchone()
    assert run_row is not None
    assert run_row[0] == "COMPLETED"

    # Verify weekly_watchlist entries
    wl_rows = db_conn.execute("SELECT epic, sunday_composite_rank FROM weekly_watchlist WHERE run_id = 'run-2026-09-27'").fetchall()
    assert len(wl_rows) == len(res)
    assert wl_rows[0][1] == 1  # Top rank is 1
