#!/usr/bin/env python3
"""
scripts/close_demo_trade.py

Completes the test trade lifecycle:
1. Identifies the active open position in SQLite.
2. Tests stop price modification via update_stop on Capital.com Demo.
3. Liquidates the active deal via close_position.
4. Transitions order status to CLOSED in SQLite.
5. Verifies 0 open positions remain on the broker.
"""
import sys
import time
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import load_config
from database.connection import get_connection
from database.repository import get_order_by_id, update_order_status
from execution.capital_client import CapitalBrokerClient


def main():
    cfg = load_config()

    if not cfg.is_demo:
        print("CRITICAL: Configuration is set to LIVE! Aborting demo script.")
        sys.exit(1)

    client = CapitalBrokerClient(
        api_key=cfg.api_key,
        identifier=cfg.identifier,
        password=cfg.password,
        demo=True,
    )

    print("=== Step 1: Inspect Active Positions on Broker ===")
    broker_positions = client.fetch_open_positions()

    if not broker_positions:
        print("No active positions currently open on Capital.com Demo.")
        print("Checking local database for orders pending closure...")
        with get_connection(cfg.db_path) as conn:
            row = conn.execute(
                "SELECT client_order_id FROM orders_and_positions WHERE order_status = 'OPEN' ORDER BY opened_at DESC LIMIT 1"
            ).fetchone()
            if row:
                cid = row["client_order_id"]
                now_ms = int(time.time() * 1000)
                update_order_status(conn, client_order_id=cid, new_status="CLOSED", close_reason="MANUAL")
                print(f"Database order '{cid}' transitioned to CLOSED.")
        return

    target_pos = broker_positions[0]
    print(f"Active Position Found:")
    print(f"  Deal ID:    {target_pos.deal_id}")
    print(f"  Epic:       {target_pos.epic}")
    print(f"  Units:      {target_pos.units}")
    print(f"  Entry Fill: {target_pos.open_level}")

    print("\n=== Step 2: Test Broker Stop Modification (update_stop) ===")
    # Ratchet stop up closer to entry price
    new_stop = round(target_pos.open_level - 3.0, 2)
    print(f"Advancing stop to: {new_stop}...")

    stop_result = client.update_stop(deal_id=target_pos.deal_id, new_stop_price=new_stop)
    if stop_result.success:
        print(f"Stop loss successfully updated on broker! Ref: {stop_result.deal_reference}")
    else:
        print(f"Stop update warning: {stop_result.reject_reason}")

    print("\n=== Step 3: Liquidate Position (close_position) ===")
    close_result = client.close_position(deal_id=target_pos.deal_id)

    if not close_result.success:
        print(f"Liquidation failed: {close_result.reject_reason}")
        sys.exit(1)

    exit_price = close_result.actual_fill_price or target_pos.open_level
    print("Position Successfully Closed on Broker!")
    print(f"  Close Deal Ref: {close_result.deal_reference}")
    print(f"  Exit Fill:      {exit_price}")

    print("\n=== Step 4: Finalize Database Order State ===")
    with get_connection(cfg.db_path) as conn:
        # Locate the local open order matching this epic
        row = conn.execute(
            "SELECT client_order_id FROM orders_and_positions WHERE order_status = 'OPEN' AND epic = ? ORDER BY opened_at DESC LIMIT 1",
            (target_pos.epic,),
        ).fetchone()

        if row:
            cid = row["client_order_id"]
            now_ms = int(time.time() * 1000)
            update_order_status(
                conn,
                client_order_id=cid,
                order_status="CLOSED",
                closed_at=now_ms,
            )
            print(f"Database order '{cid}' marked as CLOSED.")
        else:
            print("Notice: No matching open order in SQLite found to transition.")

    print("\n=== Step 5: Verify Broker Portfolio Empty ===")
    remaining = client.fetch_open_positions()
    print(f"Active positions remaining on Capital.com: {len(remaining)}")


if __name__ == "__main__":
    main()