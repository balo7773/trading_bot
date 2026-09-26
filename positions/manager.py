#!/usr/bin/env python3
"""
positions/manager.py

Tracks price watermarks and manages trailing stops for active OPEN positions.
Enforces the one-way ratchet rule: stops can only reduce risk, never increase it.
"""
import sqlite3
from typing import Optional

from database.repository import get_order_by_id, update_stop_price


def evaluate_trailing_stop(
    conn: sqlite3.Connection,
    *,
    client_order_id: str,
    current_price: float,
    trailing_distance: float,
    break_even_trigger_distance: Optional[float] = None,
) -> Optional[float]:
    """
    Evaluates an open position against incoming market prices:
    1. Verifies the order is OPEN.
    2. Updates high or low price watermarks.
    3. Calculates potential new stop price (trailing distance and break-even).
    4. Enforces the one-way ratchet: updates DB only if the new stop tightens risk.

    Returns:
        The new stop price if updated, otherwise None.
    """
    order = get_order_by_id(conn, client_order_id)
    if order is None:
        raise ValueError(f"Order not found: {client_order_id}")

    if order["order_status"] != "OPEN":
        return None

    direction = order["direction"]
    entry_price = order["actual_fill_price"] or order["planned_entry_price"]
    current_stop = order["current_stop_price"]

    # 1. Update High/Low Watermarks
    if direction == "BUY":
        current_watermark = order["trailing_high_watermark"] or entry_price
        new_watermark = max(current_watermark, current_price)

        # Baseline trailing stop: watermark minus trailing distance
        candidate_stop = round(new_watermark - trailing_distance, 5)

        # Optional Break-Even Check: if price moved far enough, stop cannot be below entry
        if break_even_trigger_distance is not None:
            if current_price >= (entry_price + break_even_trigger_distance):
                candidate_stop = max(candidate_stop, entry_price)

        # One-way ratchet: stop can ONLY move UP for a BUY
        if candidate_stop > current_stop:
            update_stop_price(
                conn,
                client_order_id=client_order_id,
                new_stop_price=candidate_stop,
                trailing_high_watermark=new_watermark,
            )
            return candidate_stop
        elif new_watermark > current_watermark:
            # Watermark rose, but stop did not cross the ratchet threshold yet
            update_stop_price(
                conn,
                client_order_id=client_order_id,
                new_stop_price=current_stop,
                trailing_high_watermark=new_watermark,
            )

    elif direction == "SELL_SHORT":
        current_watermark = order["trailing_low_watermark"] or entry_price
        new_watermark = min(current_watermark, current_price)

        # Baseline trailing stop: watermark plus trailing distance
        candidate_stop = round(new_watermark + trailing_distance, 5)

        # Optional Break-Even Check: if price dropped far enough, stop cannot be above entry
        if break_even_trigger_distance is not None:
            if current_price <= (entry_price - break_even_trigger_distance):
                candidate_stop = min(candidate_stop, entry_price)

        # One-way ratchet: stop can ONLY move DOWN for a SELL_SHORT
        if candidate_stop < current_stop:
            update_stop_price(
                conn,
                client_order_id=client_order_id,
                new_stop_price=candidate_stop,
                trailing_low_watermark=new_watermark,
            )
            return candidate_stop
        elif new_watermark < current_watermark:
            # Watermark dropped, but stop did not cross the ratchet threshold yet
            update_stop_price(
                conn,
                client_order_id=client_order_id,
                new_stop_price=current_stop,
                trailing_low_watermark=new_watermark,
            )

    return None