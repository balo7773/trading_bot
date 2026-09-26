#!/usr/bin/env python3
"""
execution/dispatcher.py

Transitions queued orders through submission and records execution results.
"""
import sqlite3
from typing import Optional

from database.repository import (
    get_order_by_id,
    record_order_fill,
    update_order_status,
)
from execution.broker import BrokerClient, ExecutionResult


def dispatch_order(
    conn: sqlite3.Connection,
    *,
    client_order_id: str,
    broker: BrokerClient,
) -> ExecutionResult:
    """
    Executes a single QUEUED order:
    1. Locks status to 'SUBMITTING'.
    2. Sends the order to the broker.
    3. On success: records fill, deal IDs, and transitions to 'OPEN'.
    4. On failure: transitions to 'ABORTED' with the broker's rejection reason.
    """
    order = get_order_by_id(conn, client_order_id)
    if order is None:
        raise ValueError(f"Order not found: {client_order_id}")

    if order["order_status"] != "QUEUED":
        raise ValueError(
            f"Cannot dispatch order {client_order_id} with status '{order['order_status']}'. Must be 'QUEUED'."
        )

    # 1. State transition: QUEUED -> SUBMITTING
    update_order_status(
        conn,
        client_order_id=client_order_id,
        new_status="SUBMITTING",
    )

    # 2. Broker execution call
    result = broker.place_order(
        client_order_id=client_order_id,
        epic=order["epic"],
        direction=order["direction"],
        units=order["allocated_units"],
        stop_price=order["initial_stop_price"],
        target_price=order["target_price"],
    )

    # 3. Post-execution status update
    if result.success:
        record_order_fill(
            conn,
            client_order_id=client_order_id,
            actual_fill_price=result.actual_fill_price,
            deal_id=result.deal_id,
            deal_reference=result.deal_reference,
        )
    else:
        # Schema constraint: ABORTED requires a valid abort_reason
        reason = result.reject_reason or "BROKER_REJECTED"
        update_order_status(
            conn,
            client_order_id=client_order_id,
            new_status="ABORTED",
            abort_reason=reason,
        )

    return result