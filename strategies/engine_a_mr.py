"""
strategies/engine_a_mr.py
Sub-Account A: Mean Reversion Strategy using TA-Lib.
"""
import time
import numpy as np
import talib
from typing import Optional
from config.enums import AbortReason, EngineType, PermittedDirection, SignalStatus, TradeDirection
from config.settings import ENGINE_A_RR_FLOOR, ENGINE_A_TARGET_CAP_PCT
from strategies.base import DailySignalCandidate


def evaluate_engine_a(
    *,
    watchlist_id: str,
    epic: str,
    signal_date: str,
    high_p: np.ndarray,
    low_p: np.ndarray,
    close_p: np.ndarray,
    permitted_direction: PermittedDirection,
    open_p: Optional[np.ndarray] = None,
    rr_floor: float = ENGINE_A_RR_FLOOR,
) -> Optional[DailySignalCandidate]:
    """
    Evaluates Engine A Mean Reversion rules at the daily close:
    - Long: Price > rising 200 SMA; bar T-1 low pierced lower BB; bar T close > lower BB.
    - Short: Price < falling 200 SMA; bar T-1 high pierced upper BB; bar T close < upper BB.
    - Stop: 3-bar extreme (bars T-2, T-1, T).
    - Target: 32 EMA frozen at close, or 5% cap from entry.
    - Floor: Initial R:R >= 1.20, else ABORTED with RR_BELOW_FLOOR.
    """
    if len(close_p) < 225:
        return None

    c_t, l_t, h_t = float(close_p[-1]), float(low_p[-1]), float(high_p[-1])
    l_t1, h_t1 = float(low_p[-2]), float(high_p[-2])
    l_t2, h_t2 = float(low_p[-3]), float(high_p[-3])

    # 1. Indicators
    sma200 = talib.SMA(close_p, timeperiod=200)
    valid_sma = sma200[-20:]
    if np.isnan(valid_sma).any():
        return None
    sma200_slope = float(np.polyfit(np.arange(20), valid_sma, 1)[0])
    is_rising = sma200_slope > 0
    is_falling = sma200_slope < 0

    ema32 = talib.EMA(close_p, timeperiod=32)
    upper_bb, mid_bb, lower_bb = talib.BBANDS(close_p, timeperiod=20, nbdevup=2.0, nbdevdn=2.0)

    direction: Optional[TradeDirection] = None

    # Long setup
    if permitted_direction in (PermittedDirection.LONG_ONLY, PermittedDirection.BOTH):
        if c_t > sma200[-1] and is_rising and l_t1 < lower_bb[-2] and c_t > lower_bb[-1]:
            direction = TradeDirection.BUY

    # Short setup
    if direction is None and permitted_direction in (PermittedDirection.SHORT_ONLY, PermittedDirection.BOTH):
        if c_t < sma200[-1] and is_falling and h_t1 > upper_bb[-2] and c_t < upper_bb[-1]:
            direction = TradeDirection.SELL_SHORT

    if direction is None:
        return None

    # 2. Stop and Target Calculation
    if direction == TradeDirection.BUY:
        stop_price = round(float(min(l_t, l_t1, l_t2)), 4)
        target_price = round(float(min(ema32[-1], c_t * (1.0 + ENGINE_A_TARGET_CAP_PCT))), 4)
        risk_dist = c_t - stop_price
        reward_dist = target_price - c_t
    else:
        stop_price = round(float(max(h_t, h_t1, h_t2)), 4)
        target_price = round(float(max(ema32[-1], c_t * (1.0 - ENGINE_A_TARGET_CAP_PCT))), 4)
        risk_dist = stop_price - c_t
        reward_dist = c_t - target_price

    if risk_dist <= 0:
        return None

    initial_rr = round(reward_dist / risk_dist, 4) if reward_dist > 0 else 0.0
    stop_dist_pct = round((risk_dist / c_t) * 100, 4)
    now_ms = int(time.time() * 1000)

    status = SignalStatus.PENDING
    abort_reason = None
    if initial_rr < rr_floor:
        status = SignalStatus.ABORTED
        abort_reason = AbortReason.RR_BELOW_FLOOR

    return DailySignalCandidate(
        watchlist_id=watchlist_id,
        epic=epic,
        engine=EngineType.MEAN_REVERSION,
        direction=direction,
        signal_date=signal_date,
        signal_close_price=c_t,
        structural_stop_price=stop_price,
        target_price=target_price,
        initial_rr_ratio=initial_rr,
        stop_distance_pct=stop_dist_pct,
        provisional_priority=initial_rr,
        signal_status=status,
        abort_reason=abort_reason,
        reclaim_deadline_at=now_ms + 3600 * 1000,
        created_at=now_ms,
    )
