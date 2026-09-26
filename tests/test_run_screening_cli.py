"""
tests/test_run_screening_cli.py
Unit tests verifying scripts/run_screening.py CLI logic:
- Scheduled vs Ad-hoc run_id formatting
- Pipeline execution and atomic DB insertion
- Data horizon handling
"""
import sqlite3
import pytest
from datetime import date
from pathlib import Path
import numpy as np
import pandas as pd

from scripts.run_screening import (
    execute_screening_pipeline,
    generate_run_id,
    load_universe_data,
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


def make_test_stock(bars: int = 250, base_price: float = 100.0, volume: float = 1_000_000.0):
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


def test_run_id_generation_scheduled_vs_adhoc():
    sched_id = generate_run_id("2026-09-27", is_adhoc=False)
    assert sched_id == "run_20260927"

    adhoc_id = generate_run_id("2026-09-30", is_adhoc=True)
    assert adhoc_id.startswith("run_20260930_ADHOC_")


def test_execute_screening_pipeline_scheduled(db_conn):
    universe = {
        "AAPL": {"df": make_test_stock(bars=250, base_price=150.0), "sector": "Tech", "region": "US"},
        "MSFT": {"df": make_test_stock(bars=250, base_price=300.0), "sector": "Tech", "region": "US"},
    }

    count = execute_screening_pipeline(
        conn=db_conn,
        target_date="2026-09-27",
        is_adhoc=False,
        universe_data=universe,
    )
    assert count > 0

    run = db_conn.execute("SELECT run_id, run_status FROM screening_runs WHERE run_id = 'run_20260927'").fetchone()
    assert run is not None
    assert run[1] == "COMPLETED"

    wl = db_conn.execute("SELECT COUNT(*) FROM weekly_watchlist WHERE run_id = 'run_20260927'").fetchone()
    assert wl[0] == count


def test_execute_screening_pipeline_adhoc_coexists_with_scheduled(db_conn):
    universe = {
        "AAPL": {"df": make_test_stock(bars=250, base_price=150.0), "sector": "Tech", "region": "US"},
    }

    # 1. Run scheduled Sunday run
    execute_screening_pipeline(
        conn=db_conn,
        target_date="2026-09-27",
        is_adhoc=False,
        universe_data=universe,
    )

    # 2. Run ad-hoc Wednesday run
    count_adhoc = execute_screening_pipeline(
        conn=db_conn,
        target_date="2026-09-30",
        is_adhoc=True,
        universe_data=universe,
    )
    assert count_adhoc > 0

    # Verify both runs coexist cleanly in the database
    runs = db_conn.execute("SELECT run_id FROM screening_runs ORDER BY started_at ASC").fetchall()
    assert len(runs) == 2
    assert runs[0][0] == "run_20260927"
    assert "ADHOC" in runs[1][0]
