"""
scripts/run_screening.py
Master Screening Runner (Spec V4.1 Section 2 & Section 8.1).
Consumes the fixed, versioned master universe from config/universe.py.
Pulls broker-native OHLCV daily bars directly from Capital.com API.
"""
import sys
from pathlib import Path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import argparse
import logging
import sqlite3
import time
from datetime import date
from typing import Any, Dict

from config import load_config
from config.universe import MASTER_UNIVERSE
from database.connection import get_connection
from execution.capital_client import CapitalBrokerClient
from screening.funnel import run_sunday_screening

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("screening_runner")


def _now_ms() -> int:
    return int(time.time() * 1000)


def generate_run_id(target_date: str, is_adhoc: bool = False) -> str:
    clean_date = target_date.replace("-", "")
    if is_adhoc:
        return f"run_{clean_date}_ADHOC_{int(time.time())}"
    return f"run_{clean_date}"


def load_universe_data(broker: CapitalBrokerClient) -> Dict[str, Dict[str, Any]]:
    """
    Streams historical daily bars directly from Capital.com for each asset in MASTER_UNIVERSE.
    Validates minimum bar count and structure before passing downstream.
    """
    universe_data: Dict[str, Dict[str, Any]] = {}
    total = len(MASTER_UNIVERSE)
    logger.info("Ingesting broker-native daily OHLCV bars for %d fixed master universe assets...", total)

    idx = 0
    for epic, meta in MASTER_UNIVERSE.items():
        idx += 1
        try:
            df = broker.fetch_historical_ohlcv(epic=epic, resolution="DAY", max_bars=1000)
            if df is not None and len(df) >= 60:
                universe_data[epic] = {
                    "df": df,
                    "sector": meta["sector"],
                    "region": meta["region"],
                }
                logger.info("[%d/%d] Ingested %s (%s - %s): %d bars, Close=%.2f",
                            idx, total, epic, meta["region"], meta["sector"], len(df), df["close"].iloc[-1])
            else:
                bars_count = len(df) if df is not None else 0
                logger.warning("[%d/%d] Incomplete history for %s (%d/60 bars) - dropped from run.",
                               idx, total, epic, bars_count)
            time.sleep(0.12)  # Respect Capital.com rate pacing (max 10 req/s)
        except Exception as e:
            logger.error("Failed to ingest %s: %s", epic, e)

    return universe_data


def main() -> None:
    parser = argparse.ArgumentParser(description="Master Screening Runner (Fixed Universe)")
    parser.add_argument("--date", type=str, default=date.today().isoformat(), help="Screening date")
    parser.add_argument("--adhoc", action="store_true", help="Tag run as ad-hoc")
    args = parser.parse_args()

    cfg = load_config()
    broker = CapitalBrokerClient(
        api_key=cfg.api_key,
        identifier=cfg.identifier,
        password=cfg.password,
        demo=cfg.is_demo,
    )

    logger.info("=== STEP 1: INGESTING BROKER BARS FOR FIXED MASTER UNIVERSE ===")
    universe_data = load_universe_data(broker)

    logger.info("=== STEP 2: EXECUTING SECTION 2 SCREENING FUNNEL ===")
    now_ms = _now_ms()
    run_id = generate_run_id(args.date, is_adhoc=args.adhoc)

    with get_connection() as conn:
        results = run_sunday_screening(
            conn=conn,
            universe_data=universe_data,
            run_id=run_id,
            run_date=args.date,
            current_time_ms=now_ms,
        )

        print("\n" + "=" * 90)
        print(f"OFFICIAL TOP 15 WATCHLIST (Run: {run_id})")
        print("=" * 90)
        print(f"{'Rank':<6}{'Epic':<10}{'Sector':<26}{'Engine':<18}{'Direction':<15}{'Score':<10}")
        print("-" * 90)
        for r in results:
            print(f"{r['rank']:<6}{r['epic']:<10}{r.get('sector', 'N/A'):<26}{r['engine']:<18}{r['direction']:<15}{r['score']:<10.2f}")
        print("=" * 90 + "\n")


if __name__ == "__main__":
    main()
