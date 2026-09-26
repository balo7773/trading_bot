"""
strategies/engine_b_bo.py
Sub-Account B: Trend Breakout Strategy using TA-Lib.
"""
import time
import numpy as np
import talib
from typing import Optional
from config.enums import EngineType, SignalStatus, TradeDirection
from strategies.base import DailySignalCandidate


def evaluate_engine_b(
    *,
    watchlist_id: str,
    epic: str,
    signal_date: str,
    high_p: np.ndarray,
    low_p: np.ndarray,
    close_p: np.ndarray,
    compression_threshold: float = 0.010,
) -> Optional[DailySignalCandidate]:
    if len(close_p) < 35:  # Need 32 for EMA and 20 for Donchian/SMA
        return None

    c_t = close_p[-1]

    # 1. Indicator Calculations
    ema32 = talib.EMA(close_p, timeperiod=32)
    sma20 = talib.SMA(close_p, timeperiod=20)
    atr14 = talib.ATR(high_p, low_p, close_p, timeperiod=14)

    # 2. Compression Gate
    compression_spread = abs(ema32[-1] - sma20[-1]) / c_t
    if compression_spread >= compression_threshold:
        return None

    # 3. 20-session Boundaries (bars T-20 through T-1, excluding current bar T)
    donchian_high = np.max(high_p[-21:-1])
    donchian_low = np.min(low_p[-21:-1])

    direction: Optional[TradeDirection] = None
    breakout_level: float = 0.0

    if c_t > donchian_high:
        direction = TradeDirection.BUY
        breakout_level = float(donchian_high)
        stop_price = round(breakout_level - 1.5 * atr14[-1], 4)
    elif c_t < donchian_low:
        direction = TradeDirection.SELL_SHORT
        breakout_level = float(donchian_low)
        stop_price = round(breakout_level + 1.5 * atr14[-1], 4)

    if direction is None:
        return None

    thrust_atr = round(abs(c_t - breakout_level) / atr14[-1], 4)
    compression_pct = round(compression_spread * 100, 4)
    stop_dist = abs(c_t - stop_price)
    stop_dist_pct = round((stop_dist / c_t) * 100, 4)
    now_ms = int(time.time() * 1000)

    return DailySignalCandidate(
        watchlist_id=watchlist_id,
        epic=epic,
        engine=EngineType.BREAKOUT,
        direction=direction,
        signal_date=signal_date,
        signal_close_price=float(c_t),
        structural_stop_price=stop_price,
        target_price=None,
        initial_rr_ratio=None,
        breakout_thrust_atr=thrust_atr,
        compression_spread_pct=compression_pct,
        stop_distance_pct=stop_dist_pct,
        provisional_priority=thrust_atr,
        signal_status=SignalStatus.PENDING,
        reclaim_deadline_at=now_ms + 3600 * 1000,
        created_at=now_ms,
    )