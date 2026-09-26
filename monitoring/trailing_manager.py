"""
monitoring/trailing_manager.py
Trade Management, Target Monitoring, and Ratcheting Trailing Stops.
Implements Master Spec V4.1 Section 4 with broker tick-size precision.
"""
from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING
from enum import StrEnum
from typing import Optional

from config.enums import CloseReason, TradeDirection


class ExitAction(StrEnum):
    HOLD = "HOLD"
    CLOSE_POSITION = "CLOSE_POSITION"
    AMEND_STOP = "AMEND_STOP"


@dataclass(frozen=True)
class ManagementAction:
    action: ExitAction
    close_reason: Optional[CloseReason] = None
    new_stop_price: Optional[float] = None
    new_high_watermark: Optional[float] = None
    new_low_watermark: Optional[float] = None
    exit_price_reference: Optional[float] = None


def evaluate_engine_a_exits(
    *,
    direction: TradeDirection,
    actual_fill_price: float,
    frozen_target_price: float,
    current_bid: float,
    current_ask: float,
    target_cap_pct: float = 0.05,
) -> ManagementAction:
    """
    Evaluates Engine A Mean Reversion exits per Section 4.1:
    - Target 1: Frozen 32 EMA (Long: Bid >= Target, Short: Ask <= Target)
    - Target 2: Hard 5.0% cap (Long: Bid >= Fill * 1.05, Short: Ask <= Fill * 0.95)
    Evaluated against raw unrounded live quotes to avoid edge-skipping.
    """
    fill = actual_fill_price
    target = frozen_target_price
    bid = current_bid
    ask = current_ask

    if direction == TradeDirection.BUY:
        cap_price = round(fill * (1.0 + target_cap_pct), 4)
        if bid >= target or bid >= cap_price:
            return ManagementAction(
                action=ExitAction.CLOSE_POSITION,
                close_reason=CloseReason.TARGET_BROKER,
                exit_price_reference=bid,
            )
    else:  # SELL_SHORT
        cap_price = round(fill * (1.0 - target_cap_pct), 4)
        if ask <= target or ask <= cap_price:
            return ManagementAction(
                action=ExitAction.CLOSE_POSITION,
                close_reason=CloseReason.TARGET_BROKER,
                exit_price_reference=ask,
            )

    return ManagementAction(action=ExitAction.HOLD)


def evaluate_engine_b_trailing_stop(
    *,
    direction: TradeDirection,
    actual_fill_price: float,
    current_stop_price: float,
    current_high: float,
    current_low: float,
    atr14: float,
    trailing_high_watermark: Optional[float] = None,
    trailing_low_watermark: Optional[float] = None,
    atr_mult: float = 1.5,
    tick_size: float = 0.01,
) -> ManagementAction:
    """
    Evaluates Engine B Breakout trailing stops per Section 4.2:
    - Long stop = highest_high_since_fill - 1.5 * ATR (floored to tick_size)
    - Short stop = lowest_low_since_fill + 1.5 * ATR (ceiled to tick_size)
    - Stops strictly tighten toward profit and NEVER loosen.
    - Watermarks are updated on both AMEND_STOP and HOLD for DB persistence.
    """
    if atr14 <= 0:
        raise ValueError("atr14 must be strictly positive.")

    curr_stop = round(current_stop_price, 4)
    stop_dist = Decimal(str(round(atr_mult * atr14, 4)))
    dec_tick = Decimal(str(tick_size))

    if direction == TradeDirection.BUY:
        base_high = trailing_high_watermark if trailing_high_watermark is not None else actual_fill_price
        new_high = round(max(base_high, current_high), 4)

        # Raw stop calculation
        raw_stop = Decimal(str(new_high)) - stop_dist
        # Floor down to tick_size so risk is never expanded
        steps = (raw_stop / dec_tick).quantize(Decimal("1"), rounding=ROUND_FLOOR)
        calculated_stop = float(steps * dec_tick)

        # Ratchet: only tighten
        if calculated_stop > curr_stop:
            return ManagementAction(
                action=ExitAction.AMEND_STOP,
                new_stop_price=calculated_stop,
                new_high_watermark=new_high,
            )
        return ManagementAction(
            action=ExitAction.HOLD,
            new_high_watermark=new_high,
        )

    else:  # SELL_SHORT
        base_low = trailing_low_watermark if trailing_low_watermark is not None else actual_fill_price
        new_low = round(min(base_low, current_low), 4)

        # Raw stop calculation
        raw_stop = Decimal(str(new_low)) + stop_dist
        # Ceil up to tick_size so risk is never expanded
        steps = (raw_stop / dec_tick).quantize(Decimal("1"), rounding=ROUND_CEILING)
        calculated_stop = float(steps * dec_tick)

        # Ratchet: only tighten
        if calculated_stop < curr_stop:
            return ManagementAction(
                action=ExitAction.AMEND_STOP,
                new_stop_price=calculated_stop,
                new_low_watermark=new_low,
            )
        return ManagementAction(
            action=ExitAction.HOLD,
            new_low_watermark=new_low,
        )
