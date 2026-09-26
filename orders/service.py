#!/usr/bin/env python3
"""
orders/service.py

Coordinates signal ingestion, pre-trade risk evaluation, and order creation.
"""
from dataclasses import dataclass
import sqlite3
from typing import Any, Optional

from database.repository import create_order
from risk.pre_trade import evaluate_pre_trade_risk


@dataclass(frozen=True)
class OrderPlacementResult:
    success: bool
    client_order_id: str
    order_data: Optional[dict[str, Any]] = None
    abort_reason: Optional[str] = None


def submit_signal_for_execution(
    conn: sqlite3.Connection,
    *,
    client_order_id: str,
    epic: str,
    sub_account: str,
    direction: str,
    planned_entry_price: float,
    stop_distance: float,
    target_distance: Optional[float] = None,
    current_spread: float,
    max_allowed_spread: float,
    risk_budget_usd: float,
    point_value_per_unit: float = 1.0,
    signal_id: Optional[str] = None,
) -> OrderPlacementResult:
    """
    Evaluates risk for a proposed signal. If approved, creates a QUEUED order in SQLite.
    If rejected, returns the failure reason without writing an open order.
    """
    # 1. Run Pre-Trade Risk Gates
    risk_result = evaluate_pre_trade_risk(
        conn,
        epic=epic,
        current_spread=current_spread,
        max_allowed_spread=max_allowed_spread,
        risk_budget_usd=risk_budget_usd,
        stop_distance=stop_distance,
        target_distance=target_distance,
        point_value_per_unit=point_value_per_unit,
    )

    if not risk_result.approved:
        return OrderPlacementResult(
            success=False,
            client_order_id=client_order_id,
            order_data=None,
            abort_reason=risk_result.abort_reason,
        )

    # 2. Derive prices based on trade direction
    if direction == "BUY":
        initial_stop_price = planned_entry_price - stop_distance
        target_price = (
            planned_entry_price + target_distance if target_distance is not None else None
        )
    elif direction == "SELL_SHORT":
        initial_stop_price = planned_entry_price + stop_distance
        target_price = (
            planned_entry_price - target_distance if target_distance is not None else None
        )
    else:
        raise ValueError(f"Unsupported trade direction: {direction}")

    # 3. Persist QUEUED order to database
    order_record = create_order(
        conn,
        client_order_id=client_order_id,
        epic=epic,
        sub_account=sub_account,
        direction=direction,
        planned_entry_price=planned_entry_price,
        initial_stop_price=initial_stop_price,
        planned_risk_r_usd=risk_result.planned_risk_r_usd,
        allocated_units=risk_result.allocated_units,
        target_price=target_price,
        signal_id=signal_id,
    )

    return OrderPlacementResult(
        success=True,
        client_order_id=client_order_id,
        order_data=order_record,
        abort_reason=None,
    )