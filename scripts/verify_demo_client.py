#!/usr/bin/env python3
"""
scripts/verify_demo_client.py

Safely tests that CapitalBrokerClient initializes from AppConfig
and queries Capital.com Demo endpoints (session and open positions).
"""
import sys
from pathlib import Path

# Add project root to sys.path so imports resolve cleanly
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import load_config
from execution.capital_client import CapitalBrokerClient


def main():
    print("1. Loading configuration from .env...")
    cfg = load_config()

    print(f"   Environment: {cfg.environment} (is_demo={cfg.is_demo})")
    print(f"   Identifier:  {cfg.identifier}")

    if not cfg.is_demo:
        print("SAFETY LOCK: Configuration is set to LIVE. Aborting test.")
        sys.exit(1)

    print("\n2. Initializing CapitalBrokerClient...")
    client = CapitalBrokerClient(
        api_key=cfg.api_key,
        identifier=cfg.identifier,
        password=cfg.password,
        demo=cfg.is_demo,
    )

    print("3. Authenticating session with Capital.com Demo...")
    headers = client._ensure_session()
    print("   Session tokens successfully obtained!")
    print(f"   CST token:             {headers['CST'][:8]}...")
    print(f"   X-SECURITY-TOKEN:      {headers['X-SECURITY-TOKEN'][:8]}...")

    print("\n4. Fetching open positions from broker...")
    positions = client.fetch_open_positions()
    print(f"   Broker reports {len(positions)} open position(s).")

    for pos in positions:
        print(f"   - Deal ID: {pos.deal_id} | Epic: {pos.epic} | Direction: {pos.direction} | Units: {pos.units}")

    print("\n CapitalBrokerClient is fully operational on Demo.")


if __name__ == "__main__":
    main()