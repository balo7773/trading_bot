#!/usr/bin/env python3
"""
database/repository.py

Data access layer. Provides clean Python functions to interact with 
the SQLite database without writing raw SQL inside trading strategies.
"""
import sqlite3
import time
from typing import Any, Optional


def current_time_ms() -> int:
    """Returns current epoch time in milliseconds."""
    return int(time.time() * 1000)


def create_order(
    conn: sqlite3.Connection,
    *,
    client_order_id: str,
    epic: str,
    sub_account: str,
    direction: str,
    planned_entry_price: float,
    initial_stop_price: float,
    planned_risk_r_usd: float,
    allocated_units: float,
    target_price: Optional[float] = None,
    signal_id: Optional[str] = None,
) -> dict[str, Any]:
    """
    Inserts a newly queued order into the orders_and_positions table.
    
    Sets initial defaults:
    - order_status: 'QUEUED'
    - current_stop_price: same as initial_stop_price
    - stop_version: 1
    - created_at / updated_at: current millisecond timestamp
    """
    now = current_time_ms()
    
    query = """
    INSERT INTO orders_and_positions (
        client_order_id,
        signal_id,
        epic,
        sub_account,
        direction,
        order_status,
        planned_entry_price,
        initial_stop_price,
        current_stop_price,
        target_price,
        stop_version,
        planned_risk_r_usd,
        allocated_units,
        created_at,
        updated_at
    ) VALUES (
        :client_order_id,
        :signal_id,
        :epic,
        :sub_account,
        :direction,
        'QUEUED',
        :planned_entry_price,
        :initial_stop_price,
        :current_stop_price,
        :target_price,
        1,
        :planned_risk_r_usd,
        :allocated_units,
        :created_at,
        :updated_at
    );
    """
    
    params = {
        "client_order_id": client_order_id,
        "signal_id": signal_id,
        "epic": epic,
        "sub_account": sub_account,
        "direction": direction,
        "planned_entry_price": planned_entry_price,
        "initial_stop_price": initial_stop_price,
        "current_stop_price": initial_stop_price,
        "target_price": target_price,
        "planned_risk_r_usd": planned_risk_r_usd,
        "allocated_units": allocated_units,
        "created_at": now,
        "updated_at": now,
    }
    
    conn.execute(query, params)
    return params


def get_order_by_id(conn: sqlite3.Connection, client_order_id: str) -> Optional[sqlite3.Row]:
    """Retrieves a single order record by its unique client_order_id."""
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM orders_and_positions WHERE client_order_id = ?;",
        (client_order_id,),
    )
    return cursor.fetchone()

def update_order_status(
    conn: sqlite3.Connection,
    *,
    client_order_id: str,
    new_status: str,
    abort_reason: Optional[str] = None,
    close_reason: Optional[str] = None,
) -> bool:
    """
    Updates the status of an existing order.
    
    Returns:
        True if the row was updated, False if client_order_id was not found.
    """
    now = current_time_ms()
    
    query = """
    UPDATE orders_and_positions
    SET order_status = :new_status,
        abort_reason = COALESCE(:abort_reason, abort_reason),
        close_reason = COALESCE(:close_reason, close_reason),
        updated_at = :updated_at
    WHERE client_order_id = :client_order_id;
    """
    
    params = {
        "client_order_id": client_order_id,
        "new_status": new_status,
        "abort_reason": abort_reason,
        "close_reason": close_reason,
        "updated_at": now,
    }
    
    cursor = conn.execute(query, params)
    return cursor.rowcount > 0

def record_order_fill(
    conn: sqlite3.Connection,
    *,
    client_order_id: str,
    actual_fill_price: float,
    deal_id: str,
    deal_reference: Optional[str] = None,
) -> bool:
    """
    Transitions an order to 'OPEN' when filled by the broker.
    Records actual fill price, broker deal references, and open timestamp.
    """
    now = current_time_ms()

    query = """
    UPDATE orders_and_positions
    SET order_status = 'OPEN',
        actual_fill_price = :actual_fill_price,
        deal_id = :deal_id,
        deal_reference = :deal_reference,
        opened_at = :opened_at,
        updated_at = :updated_at
    WHERE client_order_id = :client_order_id;
    """

    params = {
        "client_order_id": client_order_id,
        "actual_fill_price": actual_fill_price,
        "deal_id": deal_id,
        "deal_reference": deal_reference,
        "opened_at": now,
        "updated_at": now,
    }

    cursor = conn.execute(query, params)
    return cursor.rowcount > 0

def record_order_close(
    conn: sqlite3.Connection,
    *,
    client_order_id: str,
    close_reason: str,
    realized_pnl_usd: float,
    realized_pnl_r: float,
) -> bool:
    """
    Transitions an OPEN position to 'CLOSED'.
    Records final realized PnL, close reason, and close timestamp.
    """
    now = current_time_ms()

    query = """
    UPDATE orders_and_positions
    SET order_status = 'CLOSED',
        close_reason = :close_reason,
        realized_pnl_usd = :realized_pnl_usd,
        realized_pnl_r = :realized_pnl_r,
        closed_at = :closed_at,
        updated_at = :updated_at
    WHERE client_order_id = :client_order_id;
    """

    params = {
        "client_order_id": client_order_id,
        "close_reason": close_reason,
        "realized_pnl_usd": realized_pnl_usd,
        "realized_pnl_r": realized_pnl_r,
        "closed_at": now,
        "updated_at": now,
    }

    cursor = conn.execute(query, params)
    return cursor.rowcount > 0

def update_stop_price(
    conn: sqlite3.Connection,
    *,
    client_order_id: str,
    new_stop_price: float,
    trailing_high_watermark: Optional[float] = None,
    trailing_low_watermark: Optional[float] = None,
) -> bool:
    """
    Updates the stop price for an open position.
    Increments stop_version by 1 and updates price watermarks.
    """
    now = current_time_ms()

    query = """
    UPDATE orders_and_positions
    SET current_stop_price = :new_stop_price,
        stop_version = stop_version + 1,
        trailing_high_watermark = COALESCE(:trailing_high_watermark, trailing_high_watermark),
        trailing_low_watermark = COALESCE(:trailing_low_watermark, trailing_low_watermark),
        updated_at = :updated_at
    WHERE client_order_id = :client_order_id;
    """

    params = {
        "client_order_id": client_order_id,
        "new_stop_price": new_stop_price,
        "trailing_high_watermark": trailing_high_watermark,
        "trailing_low_watermark": trailing_low_watermark,
        "updated_at": now,
    }

    cursor = conn.execute(query, params)
    return cursor.rowcount > 0

def get_orders_by_status(
    conn: sqlite3.Connection,
    statuses: list[str],
) -> list[sqlite3.Row]:
    """
    Returns all orders matching any of the specified statuses,
    ordered by creation time from oldest to newest.
    """
    if not statuses:
        return []

    placeholders = ", ".join("?" for _ in statuses)
    query = f"""
    SELECT * FROM orders_and_positions
    WHERE order_status IN ({placeholders})
    ORDER BY created_at ASC;
    """

    cursor = conn.cursor()
    cursor.execute(query, statuses)
    return cursor.fetchall()

def get_order_by_deal_id(conn: sqlite3.Connection, deal_id: str) -> Optional[sqlite3.Row]:
    """Retrieves an order record using the broker's deal_id."""
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM orders_and_positions WHERE deal_id = ?;",
        (deal_id,),
    )
    return cursor.fetchone()


def get_order_by_deal_ref(conn: sqlite3.Connection, deal_reference: str) -> Optional[sqlite3.Row]:
    """Retrieves an order record using the broker's deal_reference."""
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM orders_and_positions WHERE deal_reference = ?;",
        (deal_reference,),
    )
    return cursor.fetchone()

def block_epic(
    conn: sqlite3.Connection,
    *,
    epic: str,
    reason: str,
) -> None:
    """
    Blocks a market instrument (epic) from receiving new orders.
    Replaces any existing block record for the same epic.
    """
    now = current_time_ms()
    query = """
    INSERT OR REPLACE INTO epic_blocks (epic, reason, blocked_at)
    VALUES (?, ?, ?);
    """
    conn.execute(query, (epic, reason, now))


def is_epic_blocked(conn: sqlite3.Connection, epic: str) -> bool:
    """
    Checks whether a market instrument (epic) is currently blocked.
    Returns True if a block record exists, otherwise False.
    """
    cursor = conn.cursor()
    cursor.execute(
        "SELECT 1 FROM epic_blocks WHERE epic = ? LIMIT 1;",
        (epic,),
    )
    return cursor.fetchone() is not None