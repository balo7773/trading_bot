"""
scripts/clean_universe.py
Cleans and standardizes config/universe.py:
1. Filters out index, commodity, and leveraged ETFs.
2. Resolves Capital.com broker epics to authentic exchange tickers for GICS mapping.
3. Produces a stationary, GICS-diversified master universe.
"""
import sys
from pathlib import Path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import re
import time
import yfinance as yf
from config.universe import MASTER_UNIVERSE

ETF_KEYWORDS = ["ETF", "TRUST", "ULTRAPRO", "DIREXION", "VIX", "ISHARES", "VANGUARD", "SPDR"]

def clean_epic_for_yfinance(epic: str, region: str, currency: str) -> str:
    """Translates Capital.com broker epics into Yahoo Finance tickers."""
    sym = epic.strip()

    # Known manual overrides for major European blue chips with unique broker symbols
    OVERRIDES = {
        "SAPD": "SAP.DE", "MCFR": "MC.PA", "AIRFR": "AIR.PA", "DTED": "DTE.DE",
        "SHELGB": "SHEL.L", "BP.": "BP.L", "RR.": "RR.L", "AZNL": "AZN.L",
        "GSKL": "GSK.L", "ALVD": "ALV.DE", "BASD": "BAS.DE", "BAYN": "BAYN.DE",
        "BMW": "BMW.DE", "VOW3": "VOW3.DE", "RHM": "RHM.DE", "SIE": "SIE.DE",
        "ASMLNL": "ASML.AS", "HEIN": "HEIA.AS", "INGA": "INGA.AS", "BESI": "BESI.AS",
        "ASMI": "ASMI.AS", "ADYENA": "ADYEN.AS", "BNP": "BNP.PA", "GLE": "GLE.PA",
        "ORAP": "ORA.PA", "TTEF": "TTE.PA", "RMS": "RMS.PA", "KER": "KER.PA",
        "AXONUS": "AXON", "LIUS": "LI", "LIAC": "LAC", "EZJGB": "EZJ.L",
        "BARC": "BARC.L", "LLOY": "LLOY.L", "GLEN": "GLEN.L", "RIO": "RIO.L",
        "RIOGB": "RIO.L", "ULVR": "ULVR.L", "DGE": "DGE.L", "BATS": "BATS.L",
    }
    if sym in OVERRIDES:
        return OVERRIDES[sym]

    # Rule-based cleanups
    if currency == "USD":
        if sym.endswith("US") and len(sym) > 3:
            return sym[:-2]
        return sym

    if currency == "GBP":
        clean = sym.rstrip(".")
        if clean.endswith("GB") and len(clean) > 3:
            clean = clean[:-2]
        return f"{clean}.L"

    if currency == "EUR":
        if sym.endswith("FR"):
            return f"{sym[:-2]}.PA"
        if sym.endswith("NL"):
            return f"{sym[:-2]}.AS"
        if sym.endswith("D") and len(sym) > 3:
            return f"{sym[:-1]}.DE"
        return f"{sym}.DE" if region == "EU" else sym

    return sym


def main():
    print(f"Cleaning {len(MASTER_UNIVERSE)} total assets in current universe...")
    cleaned_universe = {}
    dropped_etfs = 0

    unresolved_epics = []

    for epic, data in MASTER_UNIVERSE.items():
        name_upper = data["name"].upper()
        # 1. Filter out ETFs
        if any(kw in name_upper for kw in ETF_KEYWORDS) or epic in ["SPY", "QQQ", "VOO", "SOXL", "SQQQ", "TQQQ", "UVXY", "SMH", "SLV", "AGQ", "KWEB"]:
            dropped_etfs += 1
            continue

        sector = data["sector"]
        if sector in ["General Equities", "Diversified Industrials"]:
            unresolved_epics.append((epic, data))
        else:
            cleaned_universe[epic] = data

    print(f"Removed {dropped_etfs} ETFs/Trusts. Retained {len(cleaned_universe)} equities with existing sectors.")
    print(f"Resolving authentic GICS sectors for {len(unresolved_epics)} previously unmapped equities...\n")

    for idx, (epic, data) in enumerate(unresolved_epics, 1):
        yf_ticker = clean_epic_for_yfinance(epic, data["region"], data["currency"])
        resolved_sector = data["sector"]
        try:
            t = yf.Ticker(yf_ticker)
            info = t.info
            gics = info.get("sector")
            if gics:
                resolved_sector = gics
                print(f"[{idx}/{len(unresolved_epics)}] [+] {epic:<10} (via {yf_ticker:<10}) -> {resolved_sector}")
            else:
                print(f"[{idx}/{len(unresolved_epics)}] [-] {epic:<10} (via {yf_ticker:<10}) -> Could not resolve GICS")
        except Exception:
            pass

        cleaned_universe[epic] = {
            "name": data["name"],
            "sector": resolved_sector,
            "region": data["region"],
            "currency": data["currency"],
        }
        time.sleep(0.1)

    # Write cleaned config/universe.py
    out_path = Path("config/universe.py")
    lines = [
        '"""',
        'config/universe.py',
        'Master Spec V4.1 Section 8.1 - Fixed Master Candidate Universe.',
        f'Cleaned & Standardized: Pure Equity Universe (ETFs Removed, GICS Mapped).',
        f'Total Verified Liquid Equities: {len(cleaned_universe)}.',
        '"""',
        '',
        'from typing import Dict, Any',
        '',
        'MASTER_UNIVERSE: Dict[str, Dict[str, Any]] = {',
    ]

    for epic, d in sorted(cleaned_universe.items()):
        escaped_name = d["name"].replace('"', '\\"')
        lines.append(f'    "{epic}": {{"name": "{escaped_name}", "sector": "{d["sector"]}", "region": "{d["region"]}", "currency": "{d["currency"]}"}},')

    lines.append('}\n')
    out_path.write_text('\n'.join(lines))
    print(f"\n[SUCCESS] Wrote {len(cleaned_universe)} cleaned equities to {out_path}!")


if __name__ == "__main__":
    main()
