"""
tests/test_trailing_manager.py
Unit tests verifying Master Spec V4.1 Section 4 Trade Management & Trailing Stops:
- Engine A: Target 1 (32 EMA) & Target 2 (5% hard cap) for Long and Short
- Engine B: Watermark initialization, ratcheting stops, tick_size rounding, and pullback HOLDs
"""
import pytest
from config.enums import CloseReason, TradeDirection
from monitoring.trailing_manager import (
    ExitAction,
    evaluate_engine_a_exits,
    evaluate_engine_b_trailing_stop,
)


# ============================================================================
# Engine A: Mean Reversion Exit Tests
# ============================================================================

def test_engine_a_long_holds_when_below_targets():
    res = evaluate_engine_a_exits(
        direction=TradeDirection.BUY,
        actual_fill_price=100.0,
        frozen_target_price=103.0,
        current_bid=102.5,
        current_ask=102.6,
    )
    assert res.action == ExitAction.HOLD
    assert res.close_reason is None


def test_engine_a_long_triggers_target_1_ema():
    res = evaluate_engine_a_exits(
        direction=TradeDirection.BUY,
        actual_fill_price=100.0,
        frozen_target_price=103.0,
        current_bid=103.1,
        current_ask=103.2,
    )
    assert res.action == ExitAction.CLOSE_POSITION
    assert res.close_reason == CloseReason.TARGET_BROKER
    assert res.exit_price_reference == 103.1


def test_engine_a_long_triggers_target_2_cap():
    res = evaluate_engine_a_exits(
        direction=TradeDirection.BUY,
        actual_fill_price=100.0,
        frozen_target_price=108.0,  # Far away
        current_bid=105.05,        # Hits 5.0% cap
        current_ask=105.15,
    )
    assert res.action == ExitAction.CLOSE_POSITION
    assert res.close_reason == CloseReason.TARGET_BROKER
    assert res.exit_price_reference == 105.05


def test_engine_a_short_triggers_target_1_ema():
    res = evaluate_engine_a_exits(
        direction=TradeDirection.SELL_SHORT,
        actual_fill_price=100.0,
        frozen_target_price=97.0,
        current_bid=96.8,
        current_ask=96.9,
    )
    assert res.action == ExitAction.CLOSE_POSITION
    assert res.close_reason == CloseReason.TARGET_BROKER
    assert res.exit_price_reference == 96.9


def test_engine_a_short_triggers_target_2_cap():
    res = evaluate_engine_a_exits(
        direction=TradeDirection.SELL_SHORT,
        actual_fill_price=100.0,
        frozen_target_price=92.0,
        current_bid=94.9,
        current_ask=94.95,  # <= 5.0% cap (95.0)
    )
    assert res.action == ExitAction.CLOSE_POSITION
    assert res.close_reason == CloseReason.TARGET_BROKER


# ============================================================================
# Engine B: Breakout Trailing Stop Tests
# ============================================================================

def test_engine_b_long_initializes_watermark_and_tightens():
    res = evaluate_engine_b_trailing_stop(
        direction=TradeDirection.BUY,
        actual_fill_price=100.0,
        current_stop_price=95.0,
        current_high=102.0,
        current_low=99.0,
        atr14=4.0,  # Distance = 6.0 -> Stop = 102 - 6 = 96.0 > 95.0
        trailing_high_watermark=None,
    )
    assert res.action == ExitAction.AMEND_STOP
    assert res.new_stop_price == 96.0
    assert res.new_high_watermark == 102.0


def test_engine_b_long_tick_size_flooring():
    # High = 105.35, Distance = 6.0 -> Raw stop = 99.35.
    # tick_size = 0.10 -> Floored to 99.30 (not 99.40)
    res = evaluate_engine_b_trailing_stop(
        direction=TradeDirection.BUY,
        actual_fill_price=100.0,
        current_stop_price=95.0,
        current_high=105.35,
        current_low=102.0,
        atr14=4.0,
        trailing_high_watermark=100.0,
        tick_size=0.10,
    )
    assert res.action == ExitAction.AMEND_STOP
    assert res.new_stop_price == 99.30


def test_engine_b_long_rejects_loosening_on_pullback():
    res = evaluate_engine_b_trailing_stop(
        direction=TradeDirection.BUY,
        actual_fill_price=100.0,
        current_stop_price=99.0,
        current_high=101.0,  # Pulled back from 105.0
        current_low=97.0,
        atr14=4.0,
        trailing_high_watermark=105.0,
    )
    assert res.action == ExitAction.HOLD
    assert res.new_high_watermark == 105.0
    assert res.new_stop_price is None


def test_engine_b_short_ratchets_down():
    res = evaluate_engine_b_trailing_stop(
        direction=TradeDirection.SELL_SHORT,
        actual_fill_price=100.0,
        current_stop_price=105.0,
        current_high=98.0,
        current_low=94.0,
        atr14=4.0,  # Distance = 6.0 -> Stop = 94 + 6 = 100.0 < 105.0
        trailing_low_watermark=100.0,
    )
    assert res.action == ExitAction.AMEND_STOP
    assert res.new_stop_price == 100.0
    assert res.new_low_watermark == 94.0


def test_engine_b_invalid_atr_raises_error():
    with pytest.raises(ValueError, match="atr14 must be strictly positive"):
        evaluate_engine_b_trailing_stop(
            direction=TradeDirection.BUY,
            actual_fill_price=100.0,
            current_stop_price=95.0,
            current_high=101.0,
            current_low=99.0,
            atr14=0.0,
        )
