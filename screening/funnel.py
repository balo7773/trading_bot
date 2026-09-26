"""
screening/funnel.py
Sunday Screening Funnel, Regime Routing, Sector Capping, and Watchlist Generation.
Implements Master Spec V4.1 Section 2.
"""
import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from config.enums import EngineType, PermittedDirection
from config.settings import (
    ENGINE_A_MAX_LARGE_GAPS,
    ENGINE_A_REGIME_RSQUARED,
    ENGINE_B_COILING_SPREAD,
)

MIN_ADV_60_USD = 5000000.0  # $25M liquidity threshold
MAX_PER_SECTOR = 2             # Max 2 stocks per sector
CORRELATION_VETO_THRESHOLD = 0.70
TOP_WATCHLIST_LIMIT = 15


@dataclass(frozen=True)
class CandidateMetrics:
    epic: str
    sector: str
    region: str
    adv_60: float
    atr_pct_60d: float
    assigned_engine: EngineType
    permitted_direction: PermittedDirection
    composite_score: float
    returns_60: np.ndarray


def calculate_adv_60(df: pd.DataFrame) -> float:
    """Calculates 60-day Average Daily Dollar Volume."""
    dollar_vol = df["close"].iloc[-60:] * df["volume"].iloc[-60:]
    return float(dollar_vol.mean())


def check_engine_a_eligibility(df: pd.DataFrame, atr14_series: pd.Series) -> Tuple[bool, PermittedDirection, float]:
    """
    Evaluates Engine A Mean Reversion requirements:
    1. Max 2 opening gaps > 2.0x ATR in past 60 sessions.
    2. 200 SMA linear regression R^2 >= 0.70.
    """
    if len(df) < 200:
        return False, PermittedDirection.BOTH, 0.0

    # 1. Large Gaps Check
    recent_df = df.iloc[-60:].copy()
    recent_atr = atr14_series.iloc[-60:].values
    prev_close = df["close"].iloc[-61:-1].values
    open_price = recent_df["open"].values

    gap_sizes = np.abs(open_price - prev_close)
    large_gaps = np.sum(gap_sizes > (2.0 * recent_atr))
    if large_gaps > ENGINE_A_MAX_LARGE_GAPS:
        return False, PermittedDirection.BOTH, 0.0

    # 2. 200 SMA Linear Regression R^2 and Slope
    sma200_series = df["close"].rolling(window=200).mean().dropna()
    if len(sma200_series) < 60:
        # Check against available history if at least 200 bars exist
        sma200 = sma200_series.values
    else:
        sma200 = sma200_series.iloc[-60:].values

    x = np.arange(len(sma200))
    poly_fit = np.polyfit(x, sma200, 1)
    slope = poly_fit[0]
    p = np.poly1d(poly_fit)
    y_pred = p(x)

    y_diff = sma200 - np.mean(sma200)
    ss_tot = np.sum(y_diff ** 2)
    ss_res = np.sum((sma200 - y_pred) ** 2)
    r_squared = 1.0 - (ss_res / ss_tot) if ss_tot > 0 else 0.0

    if r_squared < ENGINE_A_REGIME_RSQUARED:
        return False, PermittedDirection.BOTH, 0.0

    # Direction from 200 SMA slope
    direction = PermittedDirection.LONG_ONLY if slope > 0 else PermittedDirection.SHORT_ONLY
    return True, direction, float(r_squared)


def check_engine_b_eligibility(df: pd.DataFrame) -> Tuple[bool, float]:
    """
    Evaluates Engine B Breakout coiling:
    Median compression of |EMA32 - SMA20| / Close over past 20 sessions < 0.015.
    """
    if len(df) < 32:
        return False, 0.0

    ema32 = df["close"].ewm(span=32, adjust=False).mean()
    sma20 = df["close"].rolling(window=20).mean()
    spread = (np.abs(ema32 - sma20) / df["close"]).iloc[-20:]
    median_spread = float(spread.median())

    if median_spread < ENGINE_B_COILING_SPREAD:
        return True, median_spread
    return False, median_spread


def filter_and_score_universe(
    universe_data: Dict[str, Dict[str, Any]]
) -> List[CandidateMetrics]:
    """
    Evaluates all raw market data against universe and regime gates:
    Returns scored candidates ready for sector capping and correlation veto.
    """
    candidates: List[CandidateMetrics] = []

    for epic, data in universe_data.items():
        df = data["df"]
        sector = data.get("sector", "Technology")
        region = data.get("region", "US")

        if len(df) < 60:
            continue

        # 1. Liquidity Gate ($25M ADV)
        adv = calculate_adv_60(df)
        if adv < MIN_ADV_60_USD:
            continue

        # Calculate Returns & ATR
        ret_60 = df["close"].pct_change().iloc[-60:].fillna(0.0).values
        atr14 = data.get("atr14_series")
        if atr14 is None:
            hl = df["high"] - df["low"]
            hc = (df["high"] - df["close"].shift(1)).abs()
            lc = (df["low"] - df["close"].shift(1)).abs()
            tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
            atr14 = tr.rolling(14).mean().bfill()

        current_close = float(df["close"].iloc[-1])
        current_atr = float(atr14.iloc[-1])
        atr_pct = round((current_atr / current_close) * 100, 4)

        # 2. Check Engine Eligibility
        is_mr, mr_dir, r2 = check_engine_a_eligibility(df, atr14)
        is_bo, coiling_spread = check_engine_b_eligibility(df)

        if not is_mr and not is_bo:
            continue

        # Score both eligibility tracks independently
        if is_mr:
            mr_score = round(r2 * 50.0 + (np.log10(adv) * 10.0), 4)
            candidates.append(
                CandidateMetrics(
                    epic=epic,
                    sector=sector,
                    region=region,
                    adv_60=adv,
                    atr_pct_60d=atr_pct,
                    assigned_engine=EngineType.MEAN_REVERSION,
                    permitted_direction=mr_dir,
                    composite_score=mr_score,
                    returns_60=ret_60,
                )
            )

        if is_bo:
            bo_score = round((1.0 / (coiling_spread + 1e-4)) * 0.5 + (np.log10(adv) * 10.0), 4)
            candidates.append(
                CandidateMetrics(
                    epic=epic,
                    sector=sector,
                    region=region,
                    adv_60=adv,
                    atr_pct_60d=atr_pct,
                    assigned_engine=EngineType.BREAKOUT,
                    permitted_direction=PermittedDirection.BOTH,
                    composite_score=bo_score,
                    returns_60=ret_60,
                )
            )

    return candidates


def apply_sector_caps_and_correlation_veto(
    candidates: List[CandidateMetrics]
) -> List[CandidateMetrics]:
    """
    Balanced portfolio pass:
    Enforces 8 Mean Reversion + 7 Breakout quota.
    Applies Correlation Veto (< 0.70) and Sector Cap (<= 2) globally.
    """
    mr_pool = [c for c in candidates if c.assigned_engine == EngineType.MEAN_REVERSION]
    bo_pool = [c for c in candidates if c.assigned_engine == EngineType.BREAKOUT]

    mr_pool.sort(key=lambda c: (-c.composite_score, -c.adv_60, c.epic))
    bo_pool.sort(key=lambda c: (-c.composite_score, -c.adv_60, c.epic))

    accepted: List[CandidateMetrics] = []
    accepted_epics = set()
    sector_counts: Dict[str, int] = {}

    def try_accept(cand: CandidateMetrics) -> bool:
        if cand.epic in accepted_epics:
            return False
        if sector_counts.get(cand.sector, 0) >= MAX_PER_SECTOR:
            return False
        for acc in accepted:
            corr = np.corrcoef(cand.returns_60, acc.returns_60)[0, 1]
            if not np.isnan(corr) and corr >= CORRELATION_VETO_THRESHOLD:
                return False
        accepted.append(cand)
        accepted_epics.add(cand.epic)
        sector_counts[cand.sector] = sector_counts.get(cand.sector, 0) + 1
        return True

    # 1. Fill Target Quotas (8 MR, 7 BO)
    mr_target = 8
    bo_target = 7

    for c in mr_pool:
        if sum(1 for a in accepted if a.assigned_engine == EngineType.MEAN_REVERSION) >= mr_target:
            break
        try_accept(c)

    for c in bo_pool:
        if sum(1 for a in accepted if a.assigned_engine == EngineType.BREAKOUT) >= bo_target:
            break
        try_accept(c)

    # 2. Fill remainder up to TOP_WATCHLIST_LIMIT if either pool fell short
    combined_remaining = [c for c in mr_pool + bo_pool if c.epic not in accepted_epics]
    combined_remaining.sort(key=lambda c: (-c.composite_score, -c.adv_60, c.epic))
    for c in combined_remaining:
        if len(accepted) >= TOP_WATCHLIST_LIMIT:
            break
        try_accept(c)

    return accepted


def run_sunday_screening(
    *,
    conn: sqlite3.Connection,
    universe_data: Dict[str, Dict[str, Any]],
    run_id: str,
    run_date: str,
    current_time_ms: int,
) -> List[dict]:
    """
    Runs full Sunday screening and commits results atomically.
    """
    # 1. Filter and Score
    initial_candidates = filter_and_score_universe(universe_data)

    # 2. Sector Cap & Correlation Veto
    final_watchlist = apply_sector_caps_and_correlation_veto(initial_candidates)

    # 3. Write to Database Atomically under BEGIN IMMEDIATE
    conn.execute("BEGIN IMMEDIATE")
    try:
        # Idempotent re-run handling: clear prior records for same run_date
        cur = conn.cursor()
        cur.execute("SELECT run_id FROM screening_runs WHERE run_date = ?", (run_date,))
        for (old_r_id,) in cur.fetchall():
            cur.execute("DELETE FROM weekly_watchlist WHERE run_id = ?", (old_r_id,))
            cur.execute("DELETE FROM screening_runs WHERE run_id = ?", (old_r_id,))

        conn.execute(
            """
            INSERT INTO screening_runs (
                run_id, run_date, run_status, started_at, completed_at, config_snapshot_json
            ) VALUES (?, ?, 'COMPLETED', ?, ?, ?)
            """,
            (
                run_id,
                run_date,
                current_time_ms,
                current_time_ms,
                json.dumps({"max_per_sector": MAX_PER_SECTOR, "corr_veto": CORRELATION_VETO_THRESHOLD}),
            ),
        )

        persisted = []
        for rank, c in enumerate(final_watchlist, start=1):
            w_id = f"WL_{run_date.replace('-', '')}_{c.epic}"
            conn.execute(
                """
                INSERT INTO weekly_watchlist (
                    watchlist_id, run_id, epic, sector, region, assigned_engine,
                    permitted_direction, sunday_composite_rank, sunday_composite_score,
                    atr_pct_60d, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    w_id,
                    run_id,
                    c.epic,
                    c.sector,
                    c.region,
                    c.assigned_engine.value,
                    c.permitted_direction.value,
                    rank,
                    c.composite_score,
                    c.atr_pct_60d,
                    current_time_ms,
                ),
            )
            persisted.append({
                "watchlist_id": w_id,
                "epic": c.epic,
                "rank": rank,
                "engine": c.assigned_engine.value,
                "direction": c.permitted_direction.value,
                "score": c.composite_score,
            })

        conn.commit()
        return persisted
    except Exception as e:
        conn.rollback()
        raise e
