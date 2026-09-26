"""
scripts/build_full_universe.py
Automated Multi-Exchange Master Universe Builder.
Harvests ~500-600 liquid equities from Capital.com's 5 premier bourses:
- US (Top 200)
- UK (Top 150)
- Germany (All ~98)
- France (All ~99)
- Netherlands (All ~46)

Enriches with authentic GICS sectors via Wikipedia index constituent tables
with yfinance fallback, verifies broker pricing status, and generates config/universe.py.
"""
import sys
from pathlib import Path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import time
import requests
import pandas as pd
import yfinance as yf
from typing import Dict, Tuple

from config import load_config
from execution.capital_client import CapitalBrokerClient

# Target Navigation Nodes & Caps
EXCHANGE_TARGETS = [
    ("hierarchy_v1.shares.us.most_traded", "US", "USD", 200),
    ("hierarchy_v1.shares.gb.most_traded", "EU", "GBP", 150),
    ("hierarchy_v1.shares.de.most_traded", "EU", "EUR", 100),
    ("hierarchy_v1.shares.fr.most_traded", "EU", "EUR", 100),
    ("hierarchy_v1.shares.nl.most_traded", "EU", "EUR", 50),
]


def load_wikipedia_sector_cache() -> Dict[str, str]:
    """Scrapes official GICS sectors for major indices into an in-memory cache."""
    cache: Dict[str, str] = {}
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

    # 1. S&P 500
    try:
        url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
        resp = requests.get(url, headers=headers, timeout=10)
        tables = pd.read_html(resp.text)
        sp500_df = tables[0]
        for _, row in sp500_df.iterrows():
            sym = str(row.get("Symbol", "")).strip().replace(".", "")
            sec = str(row.get("GICS Sector", "")).strip()
            if sym and sec:
                cache[sym] = sec
        print(f"Loaded {len(cache)} GICS sectors from S&P 500 Wikipedia table.")
    except Exception as e:
        print(f"Warning: Failed to load S&P 500 table: {e}")

    return cache


def main():
    cfg = load_config()
    broker = CapitalBrokerClient(
        api_key=cfg.api_key,
        identifier=cfg.identifier,
        password=cfg.password,
        demo=cfg.is_demo,
    )
    headers = broker._ensure_session()

    print("=== STEP 1: LOADING REFERENCE SECTOR DATA ===")
    sector_cache = load_wikipedia_sector_cache()

    print("\n=== STEP 2: CRAWLING CAPITAL.COM PREMIER BOURSES ===")
    candidate_epics = []

    for node_id, region, currency, cap in EXCHANGE_TARGETS:
        url = f"{broker.base_url}/api/v1/marketnavigation/{node_id}"
        resp = broker._http.get(url, headers=headers)
        if resp.status_code != 200:
            print(f"[-] Failed to fetch {node_id} (HTTP {resp.status_code})")
            continue

        markets = resp.json().get("markets", [])
        count = 0
        for m in markets:
            epic = m.get("epic")
            name = m.get("instrumentName", epic)
            inst_type = m.get("instrumentType", "SHARES")
            status = m.get("marketStatus")

            if not epic or inst_type != "SHARES":
                continue

            candidate_epics.append({
                "epic": epic,
                "name": name,
                "region": region,
                "currency": currency,
            })
            count += 1
            if count >= cap:
                break

        print(f"[+] Harvested {count} candidates from {node_id}")
        time.sleep(0.12)

    total_candidates = len(candidate_epics)
    print(f"\n=== STEP 3: VERIFYING {total_candidates} ASSETS & ENRICHING SECTORS ===")

    verified_universe = {}
    for idx, item in enumerate(candidate_epics, 1):
        epic = item["epic"]
        name = item["name"]
        region = item["region"]
        ccy = item["currency"]

        # 1. Verify price data on Capital.com
        url = f"{broker.base_url}/api/v1/prices/{epic}?resolution=DAY&max=5"
        resp = broker._http.get(url, headers=headers)
        if resp.status_code != 200:
            print(f"[{idx}/{total_candidates}] [-] {epic:<10} -> Capital.com HTTP {resp.status_code} (Dropped)")
            continue

        prices = resp.json().get("prices", [])
        if not prices:
            print(f"[{idx}/{total_candidates}] [-] {epic:<10} -> 0 bars (Dropped)")
            continue

        # 2. Resolve Sector: Cache -> yfinance -> Default
        clean_symbol = epic.replace(".", "").replace("US", "")
        sector = sector_cache.get(clean_symbol)

        if not sector:
            # Query yfinance fallback
            try:
                # Format Yahoo symbol
                yf_sym = epic
                if ccy == "GBP" and not epic.endswith(".L"):
                    yf_sym = f"{epic.rstrip('.')}.L"
                elif ccy == "EUR" and region == "EU":
                    yf_sym = f"{epic}.DE" if "DE" in epic or len(epic) <= 4 else f"{epic}.PA"

                t = yf.Ticker(yf_sym)
                info = t.info
                sector = info.get("sector")
            except Exception:
                sector = None

        if not sector:
            # Sector Heuristics based on common names if lookup fails
            sector = "Diversified Industrials" if "Holdings" in name or "Group" in name else "General Equities"

        verified_universe[epic] = {
            "name": name,
            "sector": sector,
            "region": region,
            "currency": ccy,
        }

        print(f"[{idx}/{total_candidates}] [+] {epic:<10} -> Verified ({region}) | Sector: {sector:<24} | Name: {name[:25]}")
        time.sleep(0.12)  # Respect Capital.com rate limits

    # 4. Write config/universe.py
    out_path = Path("config/universe.py")
    lines = [
        '"""',
        'config/universe.py',
        'Master Spec V4.1 Section 8.1 - Fixed Master Candidate Universe.',
        f'Generated via scripts/build_full_universe.py on {time.strftime("%Y-%m-%d")}.',
        f'Total Verified Liquid Assets: {len(verified_universe)}.',
        '"""',
        '',
        'from typing import Dict, Any',
        '',
        'MASTER_UNIVERSE: Dict[str, Dict[str, Any]] = {',
    ]

    for epic, data in sorted(verified_universe.items()):
        escaped_name = data["name"].replace('"', '\\"')
        lines.append(f'    "{epic}": {{"name": "{escaped_name}", "sector": "{data["sector"]}", "region": "{data["region"]}", "currency": "{data["currency"]}"}},')

    lines.append('}\n')

    out_path.write_text('\n'.join(lines))
    print(f"\n[SUCCESS] Wrote {len(verified_universe)} broker-verified assets to {out_path}!")


if __name__ == "__main__":
    main()
