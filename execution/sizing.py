"""
execution/sizing.py
Pre-Flight Risk, Exact 1R Position Sizing, and Priority Batch Allocation.
Implements Master Spec V4.1 Sections 3.4, 3.5, 3.6, 3.7, and 5.
"""
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_FLOOR
from typing import Dict, List, Optional, Tuple

from config.enums import AbortReason, SubAccount, TradeDirection
from config.settings import (
    CONCURRENT_DIRECTION_CAP,
    MAX_SPREAD_STOP_RATIO_STANDARD,
    MAX_SPREAD_STOP_RATIO_TOP,
    QUEUE_A_WEIGHT_SUNDAY,
    QUEUE_A_WEIGHT_RR,
)


@dataclass(frozen=True)
class MarketRules:
    """Instrument-specific broker parameters and market quote."""
    min_position_size: float
    lot_step: float
    margin_rate: float
    bid: float
    ask: float
    fx_rate: float = 1.0  # Multiplier to account currency (USD)


@dataclass
class BatchPortfolioState:
    """Snapshot at T0 with running in-batch reservations."""
    equity: float
    active_longs: int
    active_shorts: int
    sub_a_margin_used: float
    sub_b_margin_used: float
    
    # Running in-batch reservations
    reserved_longs: int = 0
    reserved_shorts: int = 0
    reserved_sub_a_margin: float = 0.0
    reserved_sub_b_margin: float = 0.0

    @property
    def total_longs(self) -> int:
        return self.active_longs + self.reserved_longs

    @property
    def total_shorts(self) -> int:
        return self.active_shorts + self.reserved_shorts

    @property
    def total_sub_a_margin(self) -> float:
        return self.sub_a_margin_used + self.reserved_sub_a_margin

    @property
    def total_sub_b_margin(self) -> float:
        return self.sub_b_margin_used + self.reserved_sub_b_margin

    @property
    def total_master_margin(self) -> float:
        return self.total_sub_a_margin + self.total_sub_b_margin


@dataclass(frozen=True)
class SizingResult:
    approved: bool
    allocated_units: float = 0.0
    planned_risk_r_usd: float = 0.0
    margin_required_usd: float = 0.0
    spread_to_stop_pct: float = 0.0
    abort_reason: Optional[AbortReason] = None


def check_spread_gate(
    *,
    bid: float,
    ask: float,
    stop_distance: float,
    is_top_priority: bool = False,
) -> Tuple[bool, float]:
    """
    Evaluates Section 3.4 Spread-to-Stop %:
    Spread % = (Ask - Bid) / stop_distance * 100
    Threshold is 5.0% standard, 10.0% for top priority.
    """
    if stop_distance <= 0:
        return False, 0.0

    spread = ask - bid
    spread_pct = round((spread / stop_distance) * 100, 4)
    threshold = MAX_SPREAD_STOP_RATIO_TOP if is_top_priority else MAX_SPREAD_STOP_RATIO_STANDARD

    return (spread_pct <= round(threshold * 100, 4)), spread_pct


def calculate_units_and_margin(
    *,
    equity: float,
    entry_price: float,
    stop_distance: float,
    rules: MarketRules,
) -> Tuple[float, float, float]:
    """
    Computes 1R units rounded down to lot_step via Decimal arithmetic.
    Returns: (units, actual_risk_usd, margin_required_usd)
    """
    if stop_distance <= 0 or entry_price <= 0 or equity <= 0:
        return 0.0, 0.0, 0.0

    target_r_usd = Decimal(str(round(equity * 0.01, 4)))
    dec_stop_dist = Decimal(str(round(stop_distance, 4)))
    dec_step = Decimal(str(rules.lot_step))
    dec_min_size = Decimal(str(rules.min_position_size))

    # Raw units = R / stop_distance
    raw_units = target_r_usd / dec_stop_dist

    # Floor to lot_step
    steps = (raw_units / dec_step).quantize(Decimal('1'), rounding=ROUND_FLOOR)
    units_dec = steps * dec_step

    # Enforce minimum position size
    if units_dec < dec_min_size:
        return 0.0, 0.0, 0.0

    units = float(units_dec)
    actual_risk_usd = round(units * stop_distance, 4)
    notional = units * entry_price * rules.fx_rate
    margin_required_usd = round(notional * rules.margin_rate, 4)

    return units, actual_risk_usd, margin_required_usd


def evaluate_candidate_sizing(
    *,
    sub_account: SubAccount,
    direction: TradeDirection,
    entry_price: float,
    structural_stop_price: float,
    rules: MarketRules,
    state: BatchPortfolioState,
    is_top_priority: bool = False,
) -> SizingResult:
    """
    Evaluates pre-flight risk gates for an individual candidate against the batch state:
    1. Direction Cap (<= 3 longs / 3 shorts)
    2. Spread-to-stop gate (<= 5% or 10%)
    3. 1R Position sizing & lot_step floor
    4. Sub-account (25%) and Master (50%) margin ceilings on post-rounded units
    """
    # 1. Direction Cap Check
    if direction == TradeDirection.BUY and state.total_longs >= CONCURRENT_DIRECTION_CAP:
        return SizingResult(approved=False, abort_reason=AbortReason.CONCURRENT_DIRECTION_CAP)
    if direction == TradeDirection.SELL_SHORT and state.total_shorts >= CONCURRENT_DIRECTION_CAP:
        return SizingResult(approved=False, abort_reason=AbortReason.CONCURRENT_DIRECTION_CAP)

    stop_dist = abs(entry_price - structural_stop_price)

    # 2. Spread-to-Stop Gate Check
    passed_spread, spread_pct = check_spread_gate(
        bid=rules.bid,
        ask=rules.ask,
        stop_distance=stop_dist,
        is_top_priority=is_top_priority,
    )
    if not passed_spread:
        return SizingResult(approved=False, spread_to_stop_pct=spread_pct, abort_reason=AbortReason.SPREAD_GATE_EXCEEDED)

    # 3. Position Sizing
    units, risk_usd, margin_req = calculate_units_and_margin(
        equity=state.equity,
        entry_price=entry_price,
        stop_distance=stop_dist,
        rules=rules,
    )
    if units <= 0.0:
        return SizingResult(
            approved=False,
            spread_to_stop_pct=spread_pct,
            abort_reason=AbortReason.MARGIN_CEILING_EXCEEDED,
        )

    # 4. Margin Ceiling Checks (25% Sub-Account, 50% Master)
    sub_cap = state.equity * 0.25
    master_cap = state.equity * 0.50

    if sub_account == SubAccount.SUB_A_MR:
        if state.total_sub_a_margin + margin_req > sub_cap:
            return SizingResult(approved=False, abort_reason=AbortReason.MARGIN_CEILING_EXCEEDED)
    else:
        if state.total_sub_b_margin + margin_req > sub_cap:
            return SizingResult(approved=False, abort_reason=AbortReason.MARGIN_CEILING_EXCEEDED)

    if state.total_master_margin + margin_req > master_cap:
        return SizingResult(approved=False, abort_reason=AbortReason.MARGIN_CEILING_EXCEEDED)

    # All gates passed: Reserve in batch state
    if direction == TradeDirection.BUY:
        state.reserved_longs += 1
    else:
        state.reserved_shorts += 1

    if sub_account == SubAccount.SUB_A_MR:
        state.reserved_sub_a_margin += margin_req
    else:
        state.reserved_sub_b_margin += margin_req

    return SizingResult(
        approved=True,
        allocated_units=units,
        planned_risk_r_usd=risk_usd,
        margin_required_usd=margin_req,
        spread_to_stop_pct=spread_pct,
    )


def rank_and_size_morning_batch(
    *,
    queue_a_candidates: List[dict],
    queue_b_candidates: List[dict],
    market_rules_map: Dict[str, MarketRules],
    state: BatchPortfolioState,
) -> Dict[str, SizingResult]:
    """
    Ranks Queue A and Queue B per Section 3.6, then allocates margin
    sequentially in round-robin fashion (A1, B1, A2, B2...) to prevent
    queue starvation under the Master Ceiling and Direction Cap.
    """
    # 1. Rank Queue A: Blend Sunday Rank and Executable R:R
    # Best Sunday Rank = lowest integer (1 is top)
    # Best Executable R:R = highest float
    for c in queue_a_candidates:
        c["_temp_sunday_rank"] = c.get("sunday_composite_rank", 15)
        c["_temp_exec_rr"] = c.get("executable_rr_ratio", 1.20)

    # Sort to assign ordinals 1..N
    queue_a_candidates.sort(key=lambda x: x["_temp_sunday_rank"])
    for rank_idx, c in enumerate(queue_a_candidates, start=1):
        c["_ordinal_sunday"] = rank_idx

    queue_a_candidates.sort(key=lambda x: x["_temp_exec_rr"], reverse=True)
    for rank_idx, c in enumerate(queue_a_candidates, start=1):
        c["_ordinal_rr"] = rank_idx

    for c in queue_a_candidates:
        c["final_priority"] = round(
            QUEUE_A_WEIGHT_SUNDAY * c["_ordinal_sunday"] +
            QUEUE_A_WEIGHT_RR * c["_ordinal_rr"],
            4
        )

    queue_a_candidates.sort(key=lambda x: x["final_priority"])

    # 2. Rank Queue B: Sunday rank only
    queue_b_candidates.sort(key=lambda x: x.get("sunday_composite_rank", 15))
    for c in queue_b_candidates:
        c["final_priority"] = float(c.get("sunday_composite_rank", 15))

    # 3. Interleave Queues (A1, B1, A2, B2...)
    interleaved: List[Tuple[dict, SubAccount, bool]] = []
    max_len = max(len(queue_a_candidates), len(queue_b_candidates))
    for i in range(max_len):
        if i < len(queue_a_candidates):
            interleaved.append((queue_a_candidates[i], SubAccount.SUB_A_MR, i == 0))
        if i < len(queue_b_candidates):
            interleaved.append((queue_b_candidates[i], SubAccount.SUB_B_BO, i == 0))

    # 4. Evaluate candidates sequentially with running reservations
    results: Dict[str, SizingResult] = {}
    for cand, sub_account, is_top_priority in interleaved:
        epic = cand["epic"]
        rules = market_rules_map[epic]
        res = evaluate_candidate_sizing(
            sub_account=sub_account,
            direction=cand["direction"],
            entry_price=cand["executable_entry_price"],
            structural_stop_price=cand["structural_stop_price"],
            rules=rules,
            state=state,
            is_top_priority=is_top_priority,
        )
        results[cand["signal_id"]] = res

    return results
