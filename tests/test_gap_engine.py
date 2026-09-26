"""
tests/test_gap_engine.py
Unit tests verifying Master Spec V4.1 Section 3.3 Gap Engine & Reclaim Monitor:
- Tier 1: Stop breach aborts (both directions)
- Tier 2: Exhaustion gap aborts (both directions, over-target, boundary equality)
- Tier 3: Adverse drift arms 60-min reclaim trigger
- Reclaim Monitor: Stop touch first, TTL expiry, reclaim crossing, and WAIT state
- Favorable open: Executable R:R recomputed, floor enforced (both directions)
"""
import pytest
from config.enums import AbortReason, EngineType, TradeDirection
from execution.gap_engine import GapAction, evaluate_morning_open, evaluate_reclaim_trigger

NOW = 1700000000000


# ============================================================================
# Tier 1 Tests: Adverse Stop Breach
# ============================================================================

def test_tier_1_long_stop_breach():
    res = evaluate_morning_open(
        engine=EngineType.MEAN_REVERSION,
        direction=TradeDirection.BUY,
        signal_close_price=100.0,
        structural_stop_price=95.0,
        atr14=4.0,
        live_open_price=94.0,
        current_time_ms=NOW,
        target_price=115.0,
    )
    assert res.action == GapAction.ABORT
    assert res.abort_reason == AbortReason.TIER_1_GAP_BREACH


def test_tier_1_short_stop_breach():
    res = evaluate_morning_open(
        engine=EngineType.BREAKOUT,
        direction=TradeDirection.SELL_SHORT,
        signal_close_price=100.0,
        structural_stop_price=105.0,
        atr14=4.0,
        live_open_price=106.0,
        current_time_ms=NOW,
    )
    assert res.action == GapAction.ABORT
    assert res.abort_reason == AbortReason.TIER_1_GAP_BREACH


# ============================================================================
# Tier 2 Tests: Exhaustion Gaps & Over-Target Opens
# ============================================================================

def test_tier_2_mr_exhaustion_gap_exceeded():
    res = evaluate_morning_open(
        engine=EngineType.MEAN_REVERSION,
        direction=TradeDirection.BUY,
        signal_close_price=100.0,
        structural_stop_price=95.0,
        atr14=4.0,
        live_open_price=102.5,  # Gap 2.5 > 0.5 * 4.0 = 2.0
        current_time_ms=NOW,
        target_price=115.0,
    )
    assert res.action == GapAction.ABORT
    assert res.abort_reason == AbortReason.TIER_2_EXHAUSTION


def test_tier_2_mr_open_beyond_target_long():
    res = evaluate_morning_open(
        engine=EngineType.MEAN_REVERSION,
        direction=TradeDirection.BUY,
        signal_close_price=100.0,
        structural_stop_price=95.0,
        atr14=10.0,
        live_open_price=103.5,
        current_time_ms=NOW,
        target_price=103.0,
    )
    assert res.action == GapAction.ABORT
    assert res.abort_reason == AbortReason.TIER_2_EXHAUSTION


def test_tier_2_mr_open_beyond_target_short():
    res = evaluate_morning_open(
        engine=EngineType.MEAN_REVERSION,
        direction=TradeDirection.SELL_SHORT,
        signal_close_price=100.0,
        structural_stop_price=105.0,
        atr14=10.0,
        live_open_price=96.5,  # Open below short target (97.0)
        current_time_ms=NOW,
        target_price=97.0,
    )
    assert res.action == GapAction.ABORT
    assert res.abort_reason == AbortReason.TIER_2_EXHAUSTION


def test_tier_2_bo_exhaustion_gap_exceeded():
    res = evaluate_morning_open(
        engine=EngineType.BREAKOUT,
        direction=TradeDirection.BUY,
        signal_close_price=100.0,
        structural_stop_price=94.0,
        atr14=4.0,
        live_open_price=104.5,  # Gap 4.5 > 1.0 * 4.0 = 4.0
        current_time_ms=NOW,
    )
    assert res.action == GapAction.ABORT
    assert res.abort_reason == AbortReason.TIER_2_EXHAUSTION


def test_tier_2_boundary_equality_mr_passes():
    # Gap exactly 0.5 * ATR (2.0) should NOT abort; it enters market or arms reclaim
    res = evaluate_morning_open(
        engine=EngineType.MEAN_REVERSION,
        direction=TradeDirection.BUY,
        signal_close_price=100.0,
        structural_stop_price=95.0,
        atr14=4.0,
        live_open_price=102.0,  # Gap = exactly 2.0
        current_time_ms=NOW,
        target_price=115.0,
    )
    assert res.action == GapAction.ENTER_MARKET


def test_tier_2_boundary_equality_bo_passes():
    # Gap exactly 1.0 * ATR (4.0) should NOT abort
    res = evaluate_morning_open(
        engine=EngineType.BREAKOUT,
        direction=TradeDirection.BUY,
        signal_close_price=100.0,
        structural_stop_price=94.0,
        atr14=4.0,
        live_open_price=104.0,  # Gap = exactly 4.0
        current_time_ms=NOW,
    )
    assert res.action == GapAction.ENTER_MARKET


# ============================================================================
# Tier 3 Tests: Arming the Reclaim Trigger
# ============================================================================

def test_tier_3_long_adverse_drift_arms_reclaim():
    res = evaluate_morning_open(
        engine=EngineType.MEAN_REVERSION,
        direction=TradeDirection.BUY,
        signal_close_price=100.0,
        structural_stop_price=95.0,
        atr14=4.0,
        live_open_price=98.5,
        current_time_ms=NOW,
        target_price=115.0,
    )
    assert res.action == GapAction.ARM_RECLAIM
    assert res.reclaim_trigger_price == 100.0
    assert res.reclaim_deadline_ms == NOW + 3600 * 1000


def test_tier_3_short_adverse_drift_arms_reclaim():
    res = evaluate_morning_open(
        engine=EngineType.BREAKOUT,
        direction=TradeDirection.SELL_SHORT,
        signal_close_price=100.0,
        structural_stop_price=105.0,
        atr14=4.0,
        live_open_price=101.5,
        current_time_ms=NOW,
    )
    assert res.action == GapAction.ARM_RECLAIM
    assert res.reclaim_trigger_price == 100.0
    assert res.reclaim_deadline_ms == NOW + 3600 * 1000


# ============================================================================
# Tier 3 Monitor Tests: evaluate_reclaim_trigger
# ============================================================================

def test_reclaim_monitor_long_success():
    # Reclaims at 100.20 >= 100.0
    res = evaluate_reclaim_trigger(
        engine=EngineType.MEAN_REVERSION,
        direction=TradeDirection.BUY,
        trigger_price=100.0,
        structural_stop_price=95.0,
        current_price=100.20,
        current_time_ms=NOW + 500,
        deadline_ms=NOW + 3600 * 1000,
        target_price=115.0,
    )
    assert res.action == GapAction.ENTER_MARKET
    assert res.executable_entry_price == 100.20
    assert res.executable_rr_ratio == pytest.approx(2.8462, abs=1e-3)


def test_reclaim_monitor_short_success():
    # Reclaims at 99.80 <= 100.0
    res = evaluate_reclaim_trigger(
        engine=EngineType.MEAN_REVERSION,
        direction=TradeDirection.SELL_SHORT,
        trigger_price=100.0,
        structural_stop_price=105.0,
        current_price=99.80,
        current_time_ms=NOW + 500,
        deadline_ms=NOW + 3600 * 1000,
        target_price=90.0,
    )
    assert res.action == GapAction.ENTER_MARKET
    assert res.executable_entry_price == 99.80
    assert res.executable_rr_ratio == pytest.approx(1.8846, abs=1e-3)


def test_reclaim_monitor_stop_touch_aborts():
    # Long setup: price dips to 94.90 <= stop (95.0) -> Stop breach
    res = evaluate_reclaim_trigger(
        engine=EngineType.MEAN_REVERSION,
        direction=TradeDirection.BUY,
        trigger_price=100.0,
        structural_stop_price=95.0,
        current_price=94.90,
        current_time_ms=NOW + 500,
        deadline_ms=NOW + 3600 * 1000,
        target_price=115.0,
    )
    assert res.action == GapAction.ABORT
    assert res.abort_reason == AbortReason.TIER_1_GAP_BREACH


def test_reclaim_monitor_ttl_expiry():
    # 60 minutes pass, price at 99.0 (no reclaim, no stop touch)
    res = evaluate_reclaim_trigger(
        engine=EngineType.MEAN_REVERSION,
        direction=TradeDirection.BUY,
        trigger_price=100.0,
        structural_stop_price=95.0,
        current_price=99.0,
        current_time_ms=NOW + 3600 * 1000,  # Exactly at deadline
        deadline_ms=NOW + 3600 * 1000,
        target_price=115.0,
    )
    assert res.action == GapAction.ABORT
    assert res.abort_reason == AbortReason.EXPIRED_NO_ENTRY


def test_reclaim_monitor_in_flight_wait():
    # 15 minutes in, price at 99.0 -> Continue waiting
    res = evaluate_reclaim_trigger(
        engine=EngineType.MEAN_REVERSION,
        direction=TradeDirection.BUY,
        trigger_price=100.0,
        structural_stop_price=95.0,
        current_price=99.0,
        current_time_ms=NOW + 900 * 1000,
        deadline_ms=NOW + 3600 * 1000,
        target_price=115.0,
    )
    assert res.action == GapAction.WAIT
    assert res.reclaim_deadline_ms == NOW + 3600 * 1000


# ============================================================================
# Favorable Open Tests (Both Directions) & Input Validation
# ============================================================================

def test_favorable_open_mr_short_above_floor():
    # Short: Close=100, Stop=105, Target=90. Open at 99.0.
    # Risk = 105 - 99 = 6.0, Reward = 99 - 90 = 9.0 -> RR = 1.5
    res = evaluate_morning_open(
        engine=EngineType.MEAN_REVERSION,
        direction=TradeDirection.SELL_SHORT,
        signal_close_price=100.0,
        structural_stop_price=105.0,
        atr14=4.0,
        live_open_price=99.0,
        current_time_ms=NOW,
        target_price=90.0,
    )
    assert res.action == GapAction.ENTER_MARKET
    assert res.executable_entry_price == 99.0
    assert res.executable_rr_ratio == 1.5


def test_invalid_atr_raises_error():
    with pytest.raises(ValueError, match="atr14 must be strictly positive"):
        evaluate_morning_open(
            engine=EngineType.BREAKOUT,
            direction=TradeDirection.BUY,
            signal_close_price=100.0,
            structural_stop_price=95.0,
            atr14=-1.0,
            live_open_price=101.0,
            current_time_ms=NOW,
        )
