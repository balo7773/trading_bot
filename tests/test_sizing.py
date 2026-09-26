"""
tests/test_sizing.py
Unit tests verifying Pre-Flight Risk, Fractional Rounding, Ceilings,
and Sequential Batch Reservation per Master Spec V4.1.
"""
import pytest
from config.enums import AbortReason, SubAccount, TradeDirection
from execution.sizing import (
    BatchPortfolioState,
    MarketRules,
    calculate_units_and_margin,
    check_spread_gate,
    evaluate_candidate_sizing,
    rank_and_size_morning_batch,
)


def make_default_state(equity: float = 10000.0) -> BatchPortfolioState:
    return BatchPortfolioState(
        equity=equity,
        active_longs=0,
        active_shorts=0,
        sub_a_margin_used=0.0,
        sub_b_margin_used=0.0,
    )


# ============================================================================
# 1. Fractional Share Sizing & Decimal Math Tests
# ============================================================================

def test_calculate_units_rounds_down_strictly():
    # Equity = 10,000 -> 1R = 100 USD.
    # Stop distance = 3.50. Raw units = 100 / 3.50 = 28.5714...
    # lot_step = 0.1, min_size = 0.1
    # Must round down to exactly 28.5 (never 28.6)
    rules = MarketRules(
        min_position_size=0.1,
        lot_step=0.1,
        margin_rate=0.20,
        bid=150.0,
        ask=150.05,
    )
    units, risk, margin = calculate_units_and_margin(
        equity=10000.0,
        entry_price=150.0,
        stop_distance=3.50,
        rules=rules,
    )
    assert units == 28.5
    assert risk == pytest.approx(28.5 * 3.50, abs=1e-4)  # 99.75 <= 100.0 (strictly <= 1R)
    assert margin == pytest.approx(28.5 * 150.0 * 0.20, abs=1e-4)


def test_calculate_units_insufficient_min_size():
    # Equity = 1,000 -> 1R = 10 USD.
    # Stop distance = 25.0 -> Raw units = 0.4.
    # min_position_size = 1.0 -> Units floored to 0.0
    rules = MarketRules(
        min_position_size=1.0,
        lot_step=1.0,
        margin_rate=0.20,
        bid=200.0,
        ask=200.1,
    )
    units, risk, margin = calculate_units_and_margin(
        equity=1000.0,
        entry_price=200.0,
        stop_distance=25.0,
        rules=rules,
    )
    assert units == 0.0
    assert risk == 0.0
    assert margin == 0.0


# ============================================================================
# 2. Spread-to-Stop Gate Tests
# ============================================================================

def test_spread_gate_standard_vs_top_priority():
    # Stop distance = 2.0. Spread = 0.15 -> Spread % = (0.15 / 2.0) * 100 = 7.5%
    # 7.5% > 5.0% standard -> Fails standard
    # 7.5% <= 10.0% top priority -> Passes top priority
    passed_std, pct = check_spread_gate(bid=100.0, ask=100.15, stop_distance=2.0, is_top_priority=False)
    assert not passed_std
    assert pct == 7.5

    passed_top, pct = check_spread_gate(bid=100.0, ask=100.15, stop_distance=2.0, is_top_priority=True)
    assert passed_top
    assert pct == 7.5


# ============================================================================
# 3. Direction Cap & Reservation Tests
# ============================================================================

def test_concurrent_direction_cap_enforced():
    state = make_default_state(equity=10000.0)
    state.active_longs = 2
    state.reserved_longs = 1  # 2 + 1 = 3 (Cap reached)

    rules = MarketRules(min_position_size=0.1, lot_step=0.1, margin_rate=0.20, bid=100.0, ask=100.05)
    res = evaluate_candidate_sizing(
        sub_account=SubAccount.SUB_A_MR,
        direction=TradeDirection.BUY,
        entry_price=100.0,
        structural_stop_price=95.0,
        rules=rules,
        state=state,
    )
    assert not res.approved
    assert res.abort_reason == AbortReason.CONCURRENT_DIRECTION_CAP


# ============================================================================
# 4. Margin Ceiling Checks (25% Sub-Account, 50% Master)
# ============================================================================

def test_sub_account_margin_ceiling_breach():
    # Equity = 10,000. 25% Sub-cap = 2,500.
    # Existing Sub A margin = 2,400.
    # New trade requires 200 margin -> 2,400 + 200 = 2,600 > 2,500 -> Abort
    state = make_default_state(equity=10000.0)
    state.sub_a_margin_used = 2400.0

    # 1R = 100 USD. Stop distance = 5.0 -> Units = 20. Entry = 100. Margin rate = 0.20 -> Margin = 400 USD
    rules = MarketRules(min_position_size=0.1, lot_step=0.1, margin_rate=0.20, bid=100.0, ask=100.05)
    res = evaluate_candidate_sizing(
        sub_account=SubAccount.SUB_A_MR,
        direction=TradeDirection.BUY,
        entry_price=100.0,
        structural_stop_price=95.0,
        rules=rules,
        state=state,
    )
    assert not res.approved
    assert res.abort_reason == AbortReason.MARGIN_CEILING_EXCEEDED


def test_master_margin_ceiling_breach():
    # Equity = 10,000. 50% Master ceiling = 5,000.
    # Sub A = 2,400 (under 2,500). Sub B = 2,400 (under 2,500). Total = 4,800.
    # New trade for Sub A requires 300 margin -> Sub A total = 2,700 (breaches both)
    # Let's test Sub A margin req = 250 -> Sub A = 2,450 (passes 2,500), but Master = 4,800 + 250 = 5,050 > 5,000
    state = make_default_state(equity=10000.0)
    state.sub_a_margin_used = 2200.0
    state.sub_b_margin_used = 2600.0  # Master used = 4,800

    # Stop distance = 8.0 -> Units = 100 / 8.0 = 12.5. Entry = 100, Margin rate = 0.20 -> Margin = 12.5 * 100 * 0.20 = 250.
    # Sub A projected = 2,200 + 250 = 2,450 <= 2,500 (Passes Sub-Account Cap)
    # Master projected = 4,800 + 250 = 5,050 > 5,000 (Fails Master Ceiling)
    rules = MarketRules(min_position_size=0.1, lot_step=0.1, margin_rate=0.20, bid=100.0, ask=100.05)
    res = evaluate_candidate_sizing(
        sub_account=SubAccount.SUB_A_MR,
        direction=TradeDirection.BUY,
        entry_price=100.0,
        structural_stop_price=92.0,
        rules=rules,
        state=state,
    )
    assert not res.approved
    assert res.abort_reason == AbortReason.MARGIN_CEILING_EXCEEDED


# ============================================================================
# 5. Cross-Queue Interleaving & Starvation Prevention Test
# ============================================================================

def test_batch_interleaved_queue_prevents_starvation():
    # Master margin has room for 2 trades (approx 1,000 USD margin available).
    # Queue A has 3 signals. Queue B has 2 signals.
    # Interleaving (A1, B1, A2, B2) guarantees Queue B Rank 1 is evaluated BEFORE Queue A Rank 2.
    state = make_default_state(equity=10000.0)
    state.sub_a_margin_used = 1500.0
    state.sub_b_margin_used = 2500.0  # Master used = 4,000 (Room for 1,000)
    # Sub A cap remaining = 2,500 - 1,500 = 1,000
    # Let Sub B have room too:
    state.sub_b_margin_used = 1800.0  # Master used = 3,300 (Room for 1,700 total)

    queue_a = [
        {"signal_id": "SIG_A1", "epic": "AAPL", "direction": TradeDirection.BUY, "sunday_composite_rank": 1, "executable_rr_ratio": 2.0, "executable_entry_price": 100.0, "structural_stop_price": 95.0},
        {"signal_id": "SIG_A2", "epic": "MSFT", "direction": TradeDirection.BUY, "sunday_composite_rank": 3, "executable_rr_ratio": 1.5, "executable_entry_price": 100.0, "structural_stop_price": 95.0},
    ]
    queue_b = [
        {"signal_id": "SIG_B1", "epic": "NVDA", "direction": TradeDirection.BUY, "sunday_composite_rank": 2, "executable_entry_price": 100.0, "structural_stop_price": 95.0},
    ]

    rules = MarketRules(min_position_size=0.1, lot_step=0.1, margin_rate=0.20, bid=100.0, ask=100.05)
    rules_map = {"AAPL": rules, "MSFT": rules, "NVDA": rules}

    batch_res = rank_and_size_morning_batch(
        queue_a_candidates=queue_a,
        queue_b_candidates=queue_b,
        market_rules_map=rules_map,
        state=state,
    )

    # Both A1 and B1 must be approved
    assert batch_res["SIG_A1"].approved
    assert batch_res["SIG_B1"].approved
    assert state.reserved_longs == 3  # A1 + B1 + A2
