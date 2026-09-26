#!/usr/bin/env python3
"""
scripts/inspect_market.py

Safely fetches live share parameters (trading status, pricing, and dealing rules)
from Capital.com Demo for a specific equity epic (e.g., AAPL, NVDA, TSLA).
"""
import sys
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import load_config
from execution.capital_client import CapitalBrokerClient


def main():
    epic = sys.argv[1] if len(sys.argv) > 1 else "AAPL"

    cfg = load_config()
    client = CapitalBrokerClient(
        api_key=cfg.api_key,
        identifier=cfg.identifier,
        password=cfg.password,
        demo=cfg.is_demo,
    )

    print(f"Fetching market details from Capital.com Demo for: {epic}...")
    market_data = client.get_market_rules(epic)

    instrument = market_data.get("instrument", {})
    dealing_rules = market_data.get("dealingRules", {})
    snapshot = market_data.get("snapshot", {})

    print("\n--- Instrument Info ---")
    print(f"Name:           {instrument.get('name')}")
    print(f"Epic:           {instrument.get('epic')}")
    print(f"Type:           {instrument.get('type')}")
    print(f"Currency:       {instrument.get('currency')}")

    print("\n--- Live Snapshot ---")
    print(f"Market Status:  {snapshot.get('marketStatus')} (Must be 'TRADEABLE' to execute)")
    print(f"Current Bid:    {snapshot.get('bid')}")
    print(f"Current Offer:  {snapshot.get('offer')}")
    print(f"Spread:         {round(snapshot.get('offer', 0) - snapshot.get('bid', 0), 4)}")

    print("\n--- Dealing Rules ---")
    print(f"Min Deal Size:  {dealing_rules.get('minDealSize')}")
    print(f"Min Increment:  {dealing_rules.get('minSizeIncrement')}")
    print(f"Min Stop Dist:  {dealing_rules.get('minStopOrProfitDistance')}")


if __name__ == "__main__":
    main()