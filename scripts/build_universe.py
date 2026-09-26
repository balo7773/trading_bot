"""
scripts/build_universe.py
Build-time utility to generate a verified, fixed MASTER_UNIVERSE in config/universe.py.
Fetches GICS sectors via yfinance offline and validates tradeability on Capital.com.
Run once (or quarterly), review the output, and commit to Git.
"""
import sys
from pathlib import Path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import time
import yfinance as yf
from config import load_config
from execution.capital_client import CapitalBrokerClient

# Curated candidate pool: (Capital.com Epic, yfinance Ticker, Region, Currency)
RAW_CANDIDATES = [
    # --- US MEGA / LARGE CAPS (NYSE / NASDAQ) ---
    ("AAPL", "AAPL", "US", "USD"),
    ("MSFT", "MSFT", "US", "USD"),
    ("NVDA", "NVDA", "US", "USD"),
    ("AMZN", "AMZN", "US", "USD"),
    ("GOOGL", "GOOGL", "US", "USD"),
    ("META", "META", "US", "USD"),
    ("TSLA", "TSLA", "US", "USD"),
    ("AVGO", "AVGO", "US", "USD"),
    ("ORCL", "ORCL", "US", "USD"),
    ("CRM", "CRM", "US", "USD"),
    ("AMD", "AMD", "US", "USD"),
    ("QCOM", "QCOM", "US", "USD"),
    ("INTC", "INTC", "US", "USD"),
    ("NFLX", "NFLX", "US", "USD"),
    ("DIS", "DIS", "US", "USD"),
    ("HD", "HD", "US", "USD"),
    ("NKE", "NKE", "US", "USD"),
    ("MCD", "MCD", "US", "USD"),
    ("WMT", "WMT", "US", "USD"),
    ("COST", "COST", "US", "USD"),
    ("PG", "PG", "US", "USD"),
    ("KO", "KO", "US", "USD"),
    ("JPM", "JPM", "US", "USD"),
    ("BAC", "BAC", "US", "USD"),
    ("GS", "GS", "US", "USD"),
    ("MS", "MS", "US", "USD"),
    ("V", "V", "US", "USD"),
    ("MA", "MA", "US", "USD"),
    ("LLY", "LLY", "US", "USD"),
    ("UNH", "UNH", "US", "USD"),
    ("JNJ", "JNJ", "US", "USD"),
    ("ABBV", "ABBV", "US", "USD"),
    ("MRK", "MRK", "US", "USD"),
    ("CAT", "CAT", "US", "USD"),
    ("GE", "GE", "US", "USD"),
    ("BA", "BA", "US", "USD"),
    ("HON", "HON", "US", "USD"),
    ("XOM", "XOM", "US", "USD"),
    ("CVX", "CVX", "US", "USD"),
    
    # --- EUROPE & UK BLUE CHIPS ---
    ("ASMLNL", "ASML.AS", "EU", "EUR"),
    ("SAPD", "SAP.DE", "EU", "EUR"),
    ("IFX", "IFX.DE", "EU", "EUR"),
    ("BESI", "BESI.AS", "EU", "EUR"),
    ("SIE", "SIE.DE", "EU", "EUR"),
    ("AIRFR", "AIR.PA", "EU", "EUR"),
    ("RHM", "RHM.DE", "EU", "EUR"),
    ("RR.", "RR.L", "EU", "GBP"),
    ("BNP", "BNP.PA", "EU", "EUR"),
    ("INGA", "INGA.AS", "EU", "EUR"),
    ("BARC", "BARC.L", "EU", "GBP"),
    ("LLOY", "LLOY.L", "EU", "GBP"),
    ("ALVD", "ALV.DE", "EU", "EUR"),
    ("AZNL", "AZN.L", "EU", "GBP"),
    ("GSKL", "GSK.L", "EU", "GBP"),
    ("BAYN", "BAYN.DE", "EU", "EUR"),
    ("MCFR", "MC.PA", "EU", "EUR"),
    ("RMS", "RMS.PA", "EU", "EUR"),
    ("BMW", "BMW.DE", "EU", "EUR"),
    ("VOW3", "VOW3.DE", "EU", "EUR"),
    ("ULVR", "ULVR.L", "EU", "GBP"),
    ("HEIN", "HEIA.AS", "EU", "EUR"),
    ("AD", "AD.AS", "EU", "EUR"),
    ("SHELGB", "SHEL.L", "EU", "GBP"),
    ("BP.", "BP.L", "EU", "GBP"),
    ("TTEF", "TTE.PA", "EU", "EUR"),
    ("GLEN", "GLEN.L", "EU", "GBP"),
]

def main():
    cfg = load_config()
    broker = CapitalBrokerClient(
        api_key=cfg.api_key,
        identifier=cfg.identifier,
        password=cfg.password,
        demo=cfg.is_demo,
    )
    headers = broker._ensure_session()

    print(f"Validating {len(RAW_CANDIDATES)} candidates against Capital.com API and yfinance metadata...\n")

    verified_universe = {}

    for epic, yf_ticker, region, ccy in RAW_CANDIDATES:
        # 1. Verify on Capital.com (Must return HTTP 200 with valid prices)
        url = f"{broker.base_url}/api/v1/prices/{epic}?resolution=DAY&max=5"
        resp = broker._http.get(url, headers=headers)
        if resp.status_code != 200:
            print(f"[-] {epic:<8} -> Capital.com HTTP {resp.status_code} (Skipping)")
            continue

        prices = resp.json().get("prices", [])
        if not prices:
            print(f"[-] {epic:<8} -> Capital.com returned 0 prices (Skipping)")
            continue

        # 2. Fetch official GICS Sector from yfinance
        sector = "General"
        name = epic
        try:
            t = yf.Ticker(yf_ticker)
            info = t.info
            sector = info.get("sector") or "General"
            name = info.get("shortName") or info.get("longName") or epic
        except Exception as e:
            print(f"[!] Warning fetching yfinance for {yf_ticker}: {e}")

        verified_universe[epic] = {
            "name": name,
            "sector": sector,
            "region": region,
            "currency": ccy,
        }
        print(f"[+] {epic:<8} -> Verified on Capital.com | Sector: {sector:<24} | Name: {name}")
        time.sleep(0.15)  # Pacing

    # 3. Format and write out config/universe.py
    out_path = Path("config/universe.py")
    lines = [
        '"""',
        'config/universe.py',
        'Master Spec V4.1 Section 8.1 - Fixed Master Candidate Universe.',
        'Generated via build-time verification script scripts/build_universe.py.',
        'Verified against Capital.com tradeable instruments with GICS sector mapping.',
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
    print(f"\n[SUCCESS] Successfully wrote {len(verified_universe)} verified assets to {out_path}!")

if __name__ == "__main__":
    main()
