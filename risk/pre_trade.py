#!/usr/bin/env python3
"""
risk/pre_trade.py

Evaluates signals against pre-trade safety gates (epic blocks, spread limits,
minimum reward-to-risk floor) and calculates position size using stop distance.
"""
from dataclasses import dataclass
import sqlite3
from typing import Optional

from database.repository import is_epic_blocked


@dataclass(frozen=True)
class RiskCheckResult:
    approved: bool
    allocated_units: float
    planned_risk_r_usd: float
    abort_reason: Optional[str] = None


def calculate_position_size(
    *,
    risk_budget_usd: float,
    stop_distance: float,
    point_value_per_unit: float = 1.0,
) -> float:
    """
    Computes allocated trade units strictly from dollar risk and stop distance.
    
    Formula:
        Units = Risk Budget / (Stop Distance * Point Value Per Unit)
    """
    if stop_distance <= 0:
        raise ValueError("stop_distance must be strictly positive.")
    if risk_budget_usd <= 0:
        raise ValueError("risk_budget_usd must be strictly positive.")
    if point_value_per_unit <= 0:
        raise ValueError("point_value_per_unit must be strictly positive.")

    units = risk_budget_usd / (stop_distance * point_value_per_unit)
    return round(units, 4)


def evaluate_pre_trade_risk(
    conn: sqlite3.Connection,
    *,
    epic: str,
    current_spread: float,
    max_allowed_spread: float,
    risk_budget_usd: float,
    stop_distance: float,
    target_distance: Optional[float] = None,
    min_rr_floor: float = 1.5,
    point_value_per_unit: float = 1.0,
) -> RiskCheckResult:
    """
    Runs sequential safety gates against a prospective trade signal:
    1. Instrument Block Gate: Rejects if epic is currently blocked.
    2. Spread Gate: Rejects if current spread exceeds max allowable spread.
    3. Reward-to-Risk Floor: Rejects if (target_distance / stop_distance) < min_rr_floor.
    4. Sizing: If all pass, calculates allocated units.
    """
    # Gate 1: Check if instrument is currently blocked
    if is_epic_blocked(conn, epic):
        return RiskCheckResult(
            approved=False,
            allocated_units=0.0,
            planned_risk_r_usd=0.0,
            abort_reason="CIRCUIT_BREAKER",
        )

    # Gate 2: Check spread tolerance
    if current_spread > max_allowed_spread:
        return RiskCheckResult(
            approved=False,
            allocated_units=0.0,
            planned_risk_r_usd=0.0,
            abort_reason="SPREAD_GATE_EXCEEDED",
        )

    # Gate 3: Check Reward-to-Risk floor if target distance is specified
    if target_distance is not None:
        rr_ratio = target_distance / stop_distance
        if rr_ratio < min_rr_floor:
            return RiskCheckResult(
                approved=False,
                allocated_units=0.0,
                planned_risk_r_usd=0.0,
                abort_reason="RR_BELOW_FLOOR",
            )

    # Gate 4: Position sizing from stop distance
    allocated_units = calculate_position_size(
        risk_budget_usd=risk_budget_usd,
        stop_distance=stop_distance,
        point_value_per_unit=point_value_per_unit,
    )

    return RiskCheckResult(
        approved=True,
        allocated_units=allocated_units,
        planned_risk_r_usd=risk_budget_usd,
        abort_reason=None,
    )