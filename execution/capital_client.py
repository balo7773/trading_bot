#!/usr/bin/env python3
"""
execution/capital_client.py

Capital.com REST API adapter implementing the BrokerClient protocol.
Handles session lifecycles, two-phase order execution, trailing stop updates,
position liquidations, and open position reconciliation.
"""
from dataclasses import dataclass
import time
from typing import Dict, Any, Optional, List, Dict, Any, Optional, List, Any, Optional

import httpx

from execution.broker import ExecutionResult
from reconciliation.reconciler import BrokerPosition


class CapitalAPIError(Exception):
    """Raised when Capital.com API returns an unexpected error response."""


@dataclass
class CapitalSessionTokens:
    cst: str
    security_token: str
    last_activity_time: float


class CapitalBrokerClient:
    """
    Adapter implementing BrokerClient against Capital.com REST API.
    Supports both Demo and Live trading endpoints.
    """

    DEMO_BASE_URL = "https://demo-api-capital.backend-capital.com"
    LIVE_BASE_URL = "https://api-capital.backend-capital.com"

    def __init__(
        self,
        *,
        api_key: str,
        identifier: str,
        password: str,
        demo: bool = True,
        timeout: float = 10.0,
        http_client: Optional[httpx.Client] = None,
    ):
        self.api_key = api_key
        self.identifier = identifier
        self.password = password
        self.base_url = self.DEMO_BASE_URL if demo else self.LIVE_BASE_URL
        self.timeout = timeout
        self._http = http_client or httpx.Client(timeout=self.timeout)
        self._tokens: Optional[CapitalSessionTokens] = None

    def _ensure_session(self) -> dict[str, str]:
        """
        Maintains valid session headers (CST & X-SECURITY-TOKEN).
        Refreshes tokens if none exist or if 9 minutes have elapsed since last activity.
        """
        now = time.time()
        if self._tokens and (now - self._tokens.last_activity_time) < 540:
            return {
                "CST": self._tokens.cst,
                "X-SECURITY-TOKEN": self._tokens.security_token,
            }

        url = f"{self.base_url}/api/v1/session"
        headers = {"X-CAP-API-KEY": self.api_key}
        payload = {
            "identifier": self.identifier,
            "password": self.password,
            "encryptedPassword": False,
        }

        response = self._http.post(url, headers=headers, json=payload)
        if response.status_code != 200:
            raise CapitalAPIError(
                f"Session creation failed ({response.status_code}): {response.text}"
            )

        cst = response.headers.get("CST")
        security_token = response.headers.get("X-SECURITY-TOKEN")
        if not cst or not security_token:
            raise CapitalAPIError("Response missing CST or X-SECURITY-TOKEN headers.")

        self._tokens = CapitalSessionTokens(
            cst=cst,
            security_token=security_token,
            last_activity_time=now,
        )

        return {
            "CST": self._tokens.cst,
            "X-SECURITY-TOKEN": self._tokens.security_token,
        }

    def _confirm_deal(self, deal_reference: str, auth_headers: dict[str, str]) -> ExecutionResult:
        """Helper to verify deal execution status via GET /api/v1/confirms/{dealReference}."""
        confirm_url = f"{self.base_url}/api/v1/confirms/{deal_reference}"
        try:
            confirm_resp = self._http.get(confirm_url, headers=auth_headers)
            self._tokens.last_activity_time = time.time()
        except httpx.RequestError as e:
            return ExecutionResult(
                success=False,
                deal_reference=deal_reference,
                reject_reason=f"CONFIRM_NETWORK_ERROR: {str(e)}",
            )

        if confirm_resp.status_code != 200:
            return ExecutionResult(
                success=False,
                deal_reference=deal_reference,
                reject_reason=f"CONFIRM_HTTP_{confirm_resp.status_code}: {confirm_resp.text}",
            )

        confirm_data = confirm_resp.json()
        deal_status = confirm_data.get("dealStatus")

        if deal_status == "ACCEPTED":
            return ExecutionResult(
                success=True,
                deal_id=confirm_data.get("dealId"),
                deal_reference=deal_reference,
                actual_fill_price=confirm_data.get("level"),
                reject_reason=None,
            )

        return ExecutionResult(
            success=False,
            deal_reference=deal_reference,
            reject_reason=deal_status or "REJECTED_BY_BROKER",
        )

    def place_order(
        self,
        *,
        client_order_id: str,
        epic: str,
        direction: str,
        units: float,
        stop_price: float,
        target_price: Optional[float] = None,
    ) -> ExecutionResult:
        """Executes a new market position via POST /api/v1/positions."""
        try:
            auth_headers = self._ensure_session()
        except Exception as e:
            return ExecutionResult(success=False, reject_reason=f"AUTH_FAILED: {str(e)}")

        cap_direction = "BUY" if direction == "BUY" else "SELL"
        position_payload: dict[str, Any] = {
            "epic": epic,
            "direction": cap_direction,
            "size": units,
            "stopLevel": stop_price,
        }
        if target_price is not None:
            position_payload["profitLevel"] = target_price

        pos_url = f"{self.base_url}/api/v1/positions"
        try:
            pos_resp = self._http.post(pos_url, headers=auth_headers, json=position_payload)
            self._tokens.last_activity_time = time.time()
        except httpx.RequestError as e:
            return ExecutionResult(success=False, reject_reason=f"NETWORK_ERROR: {str(e)}")

        if pos_resp.status_code != 200:
            return ExecutionResult(
                success=False,
                reject_reason=f"HTTP_{pos_resp.status_code}: {pos_resp.text}",
            )

        deal_reference = pos_resp.json().get("dealReference")
        if not deal_reference:
            return ExecutionResult(
                success=False,
                reject_reason="INVALID_RESPONSE: Missing dealReference",
            )

        return self._confirm_deal(deal_reference, auth_headers)

    def update_stop(self, *, deal_id: str, new_stop_price: float) -> ExecutionResult:
        """
        Updates the stop loss price of an active deal via PUT /api/v1/positions/{dealId}.
        """
        try:
            auth_headers = self._ensure_session()
        except Exception as e:
            return ExecutionResult(success=False, reject_reason=f"AUTH_FAILED: {str(e)}")

        url = f"{self.base_url}/api/v1/positions/{deal_id}"
        payload = {"stopLevel": new_stop_price}

        try:
            resp = self._http.put(url, headers=auth_headers, json=payload)
            self._tokens.last_activity_time = time.time()
        except httpx.RequestError as e:
            return ExecutionResult(success=False, reject_reason=f"NETWORK_ERROR: {str(e)}")

        if resp.status_code != 200:
            return ExecutionResult(
                success=False,
                reject_reason=f"HTTP_{resp.status_code}: {resp.text}",
            )

        deal_reference = resp.json().get("dealReference")
        if not deal_reference:
            return ExecutionResult(
                success=False,
                reject_reason="INVALID_RESPONSE: Missing dealReference",
            )

        return self._confirm_deal(deal_reference, auth_headers)

    def close_position(self, *, deal_id: str) -> ExecutionResult:
        """
        Closes an active position via DELETE /api/v1/positions/{dealId}.
        """
        try:
            auth_headers = self._ensure_session()
        except Exception as e:
            return ExecutionResult(success=False, reject_reason=f"AUTH_FAILED: {str(e)}")

        url = f"{self.base_url}/api/v1/positions/{deal_id}"

        try:
            resp = self._http.delete(url, headers=auth_headers)
            self._tokens.last_activity_time = time.time()
        except httpx.RequestError as e:
            return ExecutionResult(success=False, reject_reason=f"NETWORK_ERROR: {str(e)}")

        if resp.status_code != 200:
            return ExecutionResult(
                success=False,
                reject_reason=f"HTTP_{resp.status_code}: {resp.text}",
            )

        deal_reference = resp.json().get("dealReference")
        if not deal_reference:
            return ExecutionResult(
                success=False,
                reject_reason="INVALID_RESPONSE: Missing dealReference",
            )

        return self._confirm_deal(deal_reference, auth_headers)

    def fetch_open_positions(self) -> list[BrokerPosition]:
        """Queries GET /api/v1/positions and returns domain BrokerPosition records."""
        auth_headers = self._ensure_session()
        url = f"{self.base_url}/api/v1/positions"
        response = self._http.get(url, headers=auth_headers)
        self._tokens.last_activity_time = time.time()

        if response.status_code != 200:
            raise CapitalAPIError(
                f"Failed to fetch positions ({response.status_code}): {response.text}"
            )

        data = response.json()
        domain_positions: list[BrokerPosition] = []

        for item in data.get("positions", []):
            pos = item.get("position", {})
            market = item.get("market", {})
            broker_direction = pos.get("direction", "BUY")
            domain_direction = "BUY" if broker_direction == "BUY" else "SELL_SHORT"

            domain_positions.append(
                BrokerPosition(
                    deal_id=pos.get("dealId", ""),
                    epic=market.get("epic", ""),
                    direction=domain_direction,
                    units=float(pos.get("size", 0.0)),
                    open_level=float(pos.get("level", 0.0)),
                )
            )

        return domain_positions

    def get_market_rules(self, epic: str) -> dict[str, Any]:
            """
            Queries GET /api/v1/markets/{epic} to retrieve dealing rules,
            lot sizing constraints, trading status, and real-time bid/ask snapshots.
            """
            auth_headers = self._ensure_session()
            url = f"{self.base_url}/api/v1/markets/{epic}"
            resp = self._http.get(url, headers=auth_headers)
            self._tokens.last_activity_time = time.time()

            if resp.status_code != 200:
                raise CapitalAPIError(
                    f"Failed to fetch market details for {epic} ({resp.status_code}): {resp.text}"
                )

            return resp.json()
    def fetch_historical_ohlcv(self, epic: str, resolution: str = "DAY", max_bars: int = 1000):
        """
        Fetches historical OHLCV bars directly from Capital.com API.
        Computes mid-market prices from bid/ask quotes and returns a clean DataFrame.
        """
        import pandas as pd
        headers = self._ensure_session()
        url = f"{self.base_url}/api/v1/prices/{epic}?resolution={resolution}&max={max_bars}"
        resp = self._http.get(url, headers=headers)
        if resp.status_code != 200:
            return None
        data = resp.json()
        prices = data.get("prices", [])
        if not prices:
            return None
        records = []
        for p in prices:
            o = (p["openPrice"]["bid"] + p["openPrice"]["ask"]) / 2.0
            h = (p["highPrice"]["bid"] + p["highPrice"]["ask"]) / 2.0
            l = (p["lowPrice"]["bid"] + p["lowPrice"]["ask"]) / 2.0
            c = (p["closePrice"]["bid"] + p["closePrice"]["ask"]) / 2.0
            v = float(p.get("lastTradedVolume", 0.0))
            t = p.get("snapshotTimeUTC") or p.get("snapshotTime")
            records.append({"date": pd.to_datetime(t), "open": o, "high": h, "low": l, "close": c, "volume": v})
        df = pd.DataFrame(records).set_index("date").sort_index()
        return df

    def fetch_account_balance(self) -> dict:
        """
        Queries GET /api/v1/accounts using the client's active session and returns ledger data.
        """
        try:
            auth_headers = self._ensure_session()
            url = f"{self.base_url}/api/v1/accounts"
            response = self._http.get(url, headers=auth_headers)
            self._tokens.last_activity_time = time.time()

            if response.status_code != 200:
                return {
                    "status": "ERROR",
                    "error": f"HTTP {response.status_code}: {response.text}",
                    "balance": 0.0, "equity": 0.0, "available": 0.0, "pnl": 0.0
                }

            data = response.json()
            accounts = data.get("accounts", [])
            if not accounts:
                return {
                    "status": "ERROR",
                    "error": "No accounts returned in payload",
                    "balance": 0.0, "equity": 0.0, "available": 0.0, "pnl": 0.0
                }

            acc = accounts[0]
            b_info = acc.get("balance", {})
            b = float(b_info.get("balance", 0.0))
            pnl = float(b_info.get("profitLoss", 0.0))
            avail = float(b_info.get("available", 0.0))
            curr = str(b_info.get("currency", "USD"))

            return {
                "account_id": acc.get("accountId", "PRIMARY"),
                "account_name": acc.get("accountName", "Trading Account"),
                "currency": curr,
                "balance": b,
                "equity": b + pnl,
                "available": avail,
                "pnl": pnl,
                "status": "OK"
            }
        except Exception as e:
            return {
                "status": "ERROR",
                "error": str(e),
                "balance": 0.0, "equity": 0.0, "available": 0.0, "pnl": 0.0
            }
