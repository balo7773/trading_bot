"""
execution/gap_engine.py
Phase 3 Morning Execution: Gap Engine and Reclaim Trigger Evaluator.
Implements Master Spec V4.1 Section 3.3.
"""
from dataclasses import dataclass
from enum import StrEnum
from typing import Optional

from config.enums import AbortReason, EngineType, TradeDirection
from config.settings import ENGINE_A_RR_FLOOR


class GapAction(StrEnum):
    ENTER_MARKET = "ENTER_MARKET"
    ARM_RECLAIM = "ARM_RECLAIM"
    WAIT = "WAIT"
    ABORT = "ABORT"


@dataclass(frozen=True)
class GapEvaluationResult:
    action: GapAction
    abort_reason: Optional[AbortReason] = None
    executable_entry_price: Optional[float] = None
    executable_rr_ratio: Optional[float] = None
    reclaim_trigger_price: Optional[float] = None
    reclaim_deadline_ms: Optional[int] = None


def evaluate_morning_open(
    *,
    engine: EngineType,
    direction: TradeDirection,
    signal_close_price: float,
    structural_stop_price: float,
    atr14: float,
    live_open_price: float,
    current_time_ms: int,  # Mandatory to prevent silent epoch bugs
    target_price: Optional[float] = None,
    rr_floor: float = ENGINE_A_RR_FLOOR,
) -> GapEvaluationResult:
    """
    Evaluates market open against yesterday's setup according to Section 3.3:
    1. Tier 1: Open at or beyond stop -> TIER_1_GAP_BREACH
    2. Tier 2: Exhaustion gap -> TIER_2_EXHAUSTION
    3. Tier 3: Mild adverse drift -> WAITING_RECLAIM (TTL 60 min)
    4. Favorable: Recompute executable R:R for MR or accept BO
    """
    if atr14 <= 0:
        raise ValueError("atr14 must be strictly positive.")

    c = round(signal_close_price, 4)
    s = round(structural_stop_price, 4)
    o = round(live_open_price, 4)
    gap = round(abs(o - c), 4)

    # -------------------------------------------------------------------------
    # 1. TIER 1: Adverse Stop Breach
    # -------------------------------------------------------------------------
    if direction == TradeDirection.BUY and o <= s:
        return GapEvaluationResult(action=GapAction.ABORT, abort_reason=AbortReason.TIER_1_GAP_BREACH)
    if direction == TradeDirection.SELL_SHORT and o >= s:
        return GapEvaluationResult(action=GapAction.ABORT, abort_reason=AbortReason.TIER_1_GAP_BREACH)

    # -------------------------------------------------------------------------
    # 2. TIER 2: Exhaustion Gaps & Over-Target Open
    # -------------------------------------------------------------------------
    if engine == EngineType.MEAN_REVERSION:
        if target_price is None:
            raise ValueError("target_price must be provided for Engine A (Mean Reversion).")
        t = round(target_price, 4)

        # Open beyond target price is exhaustion
        if direction == TradeDirection.BUY and o >= t:
            return GapEvaluationResult(action=GapAction.ABORT, abort_reason=AbortReason.TIER_2_EXHAUSTION)
        if direction == TradeDirection.SELL_SHORT and o <= t:
            return GapEvaluationResult(action=GapAction.ABORT, abort_reason=AbortReason.TIER_2_EXHAUSTION)

        # Gap strictly exceeds 0.5 x ATR
        if gap > round(0.5 * atr14, 4):
            return GapEvaluationResult(action=GapAction.ABORT, abort_reason=AbortReason.TIER_2_EXHAUSTION)

    elif engine == EngineType.BREAKOUT:
        # Gap strictly exceeds 1.0 x ATR
        if gap > round(1.0 * atr14, 4):
            return GapEvaluationResult(action=GapAction.ABORT, abort_reason=AbortReason.TIER_2_EXHAUSTION)

    # -------------------------------------------------------------------------
    # Determine Adverse vs. Favorable Drift
    # -------------------------------------------------------------------------
    is_adverse = (o < c) if direction == TradeDirection.BUY else (o > c)

    # -------------------------------------------------------------------------
    # 3. TIER 3: Mild Adverse Drift (within 0.5 x ATR)
    # -------------------------------------------------------------------------
    if is_adverse:
        ttl_ms = current_time_ms + 3600 * 1000
        return GapEvaluationResult(
            action=GapAction.ARM_RECLAIM,
            reclaim_trigger_price=c,
            reclaim_deadline_ms=ttl_ms,
        )

    # -------------------------------------------------------------------------
    # 4. FAVORABLE / FLAT OPEN: Immediate Market Entry
    # -------------------------------------------------------------------------
    if engine == EngineType.MEAN_REVERSION:
        assert target_price is not None, "target_price missing for MR"
        t = round(target_price, 4)

        # Direction-aware calculation with explicit positive sign validation
        if direction == TradeDirection.BUY:
            risk = round(o - s, 4)
            reward = round(t - o, 4)
        else:
            risk = round(s - o, 4)
            reward = round(o - t, 4)

        assert risk > 0, f"Invalid risk sign after gap gates: o={o}, s={s}, dir={direction}"
        assert reward > 0, f"Invalid reward sign after gap gates: o={o}, target={t}, dir={direction}"

        exec_rr = round(reward / risk, 4)
        if exec_rr < rr_floor:
            return GapEvaluationResult(
                action=GapAction.ABORT,
                abort_reason=AbortReason.RR_BELOW_FLOOR,
                executable_entry_price=o,
                executable_rr_ratio=exec_rr,
            )

        return GapEvaluationResult(
            action=GapAction.ENTER_MARKET,
            executable_entry_price=o,
            executable_rr_ratio=exec_rr,
        )

    # Engine B Breakout
    return GapEvaluationResult(
        action=GapAction.ENTER_MARKET,
        executable_entry_price=o,
    )


def evaluate_reclaim_trigger(
    *,
    engine: EngineType,
    direction: TradeDirection,
    trigger_price: float,         # Yesterday's close C
    structural_stop_price: float,  # S
    current_price: float,          # Live tick/bar price
    current_time_ms: int,          # Current timestamp
    deadline_ms: int,              # Expiry timestamp
    target_price: Optional[float] = None,
    rr_floor: float = ENGINE_A_RR_FLOOR,
) -> GapEvaluationResult:
    """
    Monitors an armed reclaim trigger during the 60-minute window per Section 3.3:
    1. Stop-touch first -> Abort: TIER_1_GAP_BREACH (wins all ties)
    2. TTL expires -> Abort: EXPIRED_NO_ENTRY
    3. Price crosses trigger price (C) -> ENTER_MARKET (recomputed R:R for MR)
    4. Otherwise -> WAIT
    """
    p = round(current_price, 4)
    s = round(structural_stop_price, 4)
    c = round(trigger_price, 4)

    # 1. Stop touch wins all ties and precedence
    if (direction == TradeDirection.BUY and p <= s) or (direction == TradeDirection.SELL_SHORT and p >= s):
        return GapEvaluationResult(action=GapAction.ABORT, abort_reason=AbortReason.TIER_1_GAP_BREACH)

    # 2. TTL Expiry check
    if current_time_ms >= deadline_ms:
        return GapEvaluationResult(action=GapAction.ABORT, abort_reason=AbortReason.EXPIRED_NO_ENTRY)

    # 3. Reclaim crossing check
    is_reclaimed = (p >= c) if direction == TradeDirection.BUY else (p <= c)
    if is_reclaimed:
        if engine == EngineType.MEAN_REVERSION:
            if target_price is None:
                raise ValueError("target_price must be provided for Engine A.")
            t = round(target_price, 4)

            if direction == TradeDirection.BUY:
                risk = round(p - s, 4)
                reward = round(t - p, 4)
            else:
                risk = round(s - p, 4)
                reward = round(p - t, 4)

            if risk <= 0 or reward <= 0:
                return GapEvaluationResult(action=GapAction.ABORT, abort_reason=AbortReason.RR_BELOW_FLOOR)

            exec_rr = round(reward / risk, 4)
            if exec_rr < rr_floor:
                return GapEvaluationResult(
                    action=GapAction.ABORT,
                    abort_reason=AbortReason.RR_BELOW_FLOOR,
                    executable_entry_price=p,
                    executable_rr_ratio=exec_rr,
                )

            return GapEvaluationResult(
                action=GapAction.ENTER_MARKET,
                executable_entry_price=p,
                executable_rr_ratio=exec_rr,
            )

        # Breakout reclaim
        return GapEvaluationResult(
            action=GapAction.ENTER_MARKET,
            executable_entry_price=p,
        )

    # 4. In-flight: continue waiting
    return GapEvaluationResult(
        action=GapAction.WAIT,
        reclaim_trigger_price=c,
        reclaim_deadline_ms=deadline_ms,
    )
