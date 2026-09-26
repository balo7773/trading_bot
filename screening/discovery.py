"""
screening/discovery.py
Dynamic Multi-Exchange Market Discovery Engine.
Crawls Capital.com market navigation tree across the top 5 global bourses:
- United States (NYSE/NASDAQ)
- United Kingdom (LSE)
- Germany (XETRA)
- France (Euronext Paris)
- Netherlands (Euronext Amsterdam)
"""
import logging
import time
from typing import Dict, List, Any
from execution.capital_client import CapitalBrokerClient

logger = logging.getLogger("market_discovery")

# Target Global Exchanges
GLOBAL_EXCHANGE_NODES = {
    "US": "hierarchy_v1.shares.us.most_traded",
    "UK": "hierarchy_v1.shares.gb.most_traded",
    "Germany": "hierarchy_v1.shares.de.most_traded",
    "France": "hierarchy_v1.shares.fr.most_traded",
    "Netherlands": "hierarchy_v1.shares.nl.most_traded",
}

def discover_global_equities(
    broker: CapitalBrokerClient,
    max_per_exchange: int = 40,
) -> Dict[str, Dict[str, Any]]:
    """
    Crawls Capital.com API to discover liquid candidate equities across the top 5 bourses.
    Returns mapping: {epic: {'name': str, 'region': str, 'sector': str}}
    """
    headers = broker._ensure_session()
    discovered: Dict[str, Dict[str, Any]] = {}

    for region, node_id in GLOBAL_EXCHANGE_NODES.items():
        logger.info("Crawling %s equities from node %s...", region, node_id)
        url = f"{broker.base_url}/api/v1/marketnavigation/{node_id}"
        resp = broker._http.get(url, headers=headers)

        if resp.status_code != 200:
            logger.warning("Failed to fetch %s (HTTP %d)", region, resp.status_code)
            continue

        markets = resp.json().get("markets", [])
        logger.info("Found %d total markets in %s node.", len(markets), region)

        # Select candidates up to quota per exchange
        count = 0
        for m in markets:
            epic = m.get("epic")
            name = m.get("instrumentName", epic)
            instrument_type = m.get("instrumentType", "SHARES")

            if not epic or instrument_type != "SHARES":
                continue

            discovered[epic] = {
                "name": name,
                "region": region,
                "sector": f"{region} Equities",
            }
            count += 1
            if count >= max_per_exchange:
                break

        logger.info("Sampled top %d %s equities.", count, region)
        time.sleep(0.12)  # Respect Capital.com rate limits

    logger.info("Total dynamic universe discovered: %d equities across 5 exchanges.", len(discovered))
    return discovered
