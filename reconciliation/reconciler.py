#!/usr/bin/env python3
"""
reconciliation/reconciler.py

Synchronizes local database state with live broker positions.
Detects server-side closes and halts trading on instruments with untracked manual trades.
"""
from dataclasses import dataclass, field
import sqlite3
from typing import Optional

from database.repository import (
    block_epic,
    get_orders_by_status,
    record_order_close,
)


@dataclass(frozen=True)
class BrokerPosition:
    deal_id: str
    epic: str
    direction: str
    units: float
    open_level: float


@dataclass
class ReconciliationReport:
    matched_count: int = 0
    closed_orders: list[str] = field(default_factory=list)
    blocked_epics: list[str] = field(default_factory=list)


def reconcile_positions(
    conn: sqlite3.Connection,
    *,
    broker_positions: list[BrokerPosition],
) -> ReconciliationReport:
    """
    Compares local OPEN orders against actual broker positions:
    1. If a local order's deal_id is missing from broker positions, it closed remotely.
       -> Mark local order as CLOSED.
    2. If a broker position has no matching local order, it is an untracked/manual trade.
       -> Block the epic with MANUAL_POSITION_EXISTS to protect risk budget.
    """
    report = ReconciliationReport()

    # 1. Fetch all locally tracked OPEN positions
    local_open_orders = get_orders_by_status(conn, ["OPEN"])
    local_deals = {order["deal_id"]: order for order in local_open_orders if order["deal_id"]}
    broker_deals = {pos.deal_id: pos for pos in broker_positions}

    # 2. Check for locally OPEN orders that no longer exist on broker (remotely closed)
    for deal_id, order in local_deals.items():
        if deal_id not in broker_deals:
            # Position closed at broker (e.g. stop hit or target reached)
            client_order_id = order["client_order_id"]
            record_order_close(
                conn,
                client_order_id=client_order_id,
                close_reason="STOP_BROKER",
                realized_pnl_usd=0.0,
                realized_pnl_r=0.0,
            )
            report.closed_orders.append(client_order_id)
        else:
            report.matched_count += 1

    # 3. Check for broker positions not tracked locally (untracked / manual positions)
    for deal_id, broker_pos in broker_deals.items():
        if deal_id not in local_deals:
            block_epic(
                conn,
                epic=broker_pos.epic,
                reason="MANUAL_POSITION_EXISTS",
            )
            report.blocked_epics.append(broker_pos.epic)

    return report