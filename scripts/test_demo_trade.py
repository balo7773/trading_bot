#!/usr/bin/env python3
"""
scripts/test_demo_trade.py

Executes a controlled 0.1 share test trade on AAPL in Capital.com DEMO:
1. Validates fail-safe DEMO configuration and market TRADEABLE status.
2. Ingests signal and queues order in SQLite via submit_signal_for_execution.
3. Dispatches order via CapitalBrokerClient to Capital.com Demo matching engine.
4. Confirms fill, updates local DB to OPEN, and verifies via fetch_open_positions.
"""
import sys
import time
from pathlib import Path

# Add project root to sys.path so modules resolve cleanly
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import load_config
from database.connection import get_connection, init_db
from database.repository import get_order_by_id
from execution.capital_client import CapitalBrokerClient
from execution.dispatcher import dispatch_order
from orders.service import submit_signal_for_execution


def main():
    cfg = load_config()

    # 1. Hard Safety Lock
    if not cfg.is_demo:
        print("CRITICAL: Configuration is set to LIVE! Aborting demo test.")
        sys.exit(1)

    print("=== Step 1: Initialize Database & Demo Client ===")
    init_db(cfg.db_path)
    client = CapitalBrokerClient(
        api_key=cfg.api_key,
        identifier=cfg.identifier,
        password=cfg.password,
        demo=True,
    )
    print(f"Connected to database: {cfg.db_path}")

    print("\n=== Step 2: Query Live AAPL Market Rules & Snapshot ===")
    epic = "AAPL"
    market_data = client.get_market_rules(epic)
    snapshot = market_data.get("snapshot", {})

    market_status = snapshot.get("marketStatus")
    bid = float(snapshot.get("bid", 0.0))
    offer = float(snapshot.get("offer", 0.0))
    spread = round(offer - bid, 4)

    print(f"Market Status: {market_status}")
    print(f"Current Bid:   {bid}")
    print(f"Current Offer: {offer}")
    print(f"Spread:        {spread}")

    if market_status != "TRADEABLE":
        print(f"\nExchange is currently {market_status}. Market orders will be rejected.")
        print("Please run this script during US market trading hours.")
        sys.exit(0)

    # 2. Parameters for 0.1 Share Test Order
    # Stop distance = $5.00. Sizing 0.1 units means risk budget is $0.50 ($5.00 * 0.1)
    stop_distance = 5.0
    target_distance = 10.0
    risk_budget_usd = 0.50
    point_value = 1.0
    client_order_id = f"DEMO-AAPL-{int(time.time())}"

    print(f"\n=== Step 3: Queue Order in Local Database ({client_order_id}) ===")
    with get_connection(cfg.db_path) as conn:
        placement = submit_signal_for_execution(
            conn,
            client_order_id=client_order_id,
            epic=epic,
            sub_account="SUB_A_MR",
            direction="BUY",
            planned_entry_price=offer,
            stop_distance=stop_distance,
            target_distance=target_distance,
            current_spread=spread,
            max_allowed_spread=max(2.0, spread * 1.5),
            risk_budget_usd=risk_budget_usd,
            point_value_per_unit=point_value,
        )

        if not placement.success:
            print(f"Pre-trade risk rejected order: {placement.reject_reason}")
            sys.exit(1)

        queued_order = get_order_by_id(conn, client_order_id)
        print("Order successfully QUEUED in SQLite:")
        print(f"  Allocated Units: {queued_order['allocated_units']}")
        print(f"  Initial Stop:    {queued_order['initial_stop_price']}")
        print(f"  Target Price:    {queued_order['target_price']}")

    print("\n=== Step 4: Dispatch Order to Capital.com Demo ===")
    with get_connection(cfg.db_path) as conn:
        exec_result = dispatch_order(
            conn,
            client_order_id=client_order_id,
            broker=client,
        )

        if not exec_result.success:
            print(f"Dispatch failed: {exec_result.reject_reason}")
            sys.exit(1)

        print("Execution Successful!")
        print(f"  Deal Reference:    {exec_result.deal_reference}")
        print(f"  Broker Deal ID:    {exec_result.deal_id}")
        print(f"  Actual Fill Price: {exec_result.actual_fill_price}")

        # Verify local database order transition
        order_record = get_order_by_id(conn, client_order_id)
        print(f"\n=== Step 5: Verified Local Order State ===")
        print(f"  Order Status: {order_record['order_status']}")
        print(f"  Opened At:    {order_record['opened_at']}")

    print("\n=== Step 6: Verify Active Positions on Capital.com Broker ===")
    active_positions = client.fetch_open_positions()
    print(f"Broker reports {len(active_positions)} active position(s):")
    for pos in active_positions:
        print(f"  - Deal ID: {pos.deal_id} | Epic: {pos.epic} | Units: {pos.units} | Level: {pos.open_level}")


if __name__ == "__main__":
    main()