#!/usr/bin/env python3
"""
execution/broker.py

Defines the broker execution interface and a deterministic mock client for testing.
"""
from dataclasses import dataclass
from typing import Optional, Protocol


@dataclass(frozen=True)
class ExecutionResult:
    success: bool
    deal_id: Optional[str] = None
    deal_reference: Optional[str] = None
    actual_fill_price: Optional[float] = None
    reject_reason: Optional[str] = None


class BrokerClient(Protocol):
    """Protocol defining the standard methods any broker implementation must provide."""

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
        ...


class MockBrokerClient:
    """Deterministic mock broker allowing test suites to inject fills or rejections."""

    def __init__(self, simulate_rejection: bool = False, reject_reason: str = "BROKER_REJECTED", slippage: float = 0.0):
        self.simulate_rejection = simulate_rejection
        self.reject_reason = reject_reason
        self.slippage = slippage
        self.placed_orders: list[dict] = []

    def place_order(
        self,
        *,
        client_order_id: str,
        epic: str,
        direction: str,
        units: float,
        stop_price: float,
        target_price: Optional[float] = None,
        base_fill_price: float = 1.0850,
    ) -> ExecutionResult:
        self.placed_orders.append({
            "client_order_id": client_order_id,
            "epic": epic,
            "direction": direction,
            "units": units,
            "stop_price": stop_price,
            "target_price": target_price,
        })

        if self.simulate_rejection:
            return ExecutionResult(
                success=False,
                reject_reason=self.reject_reason,
            )

        actual_fill = base_fill_price + self.slippage if direction == "BUY" else base_fill_price - self.slippage
        return ExecutionResult(
            success=True,
            deal_id=f"DEAL-{client_order_id}",
            deal_reference=f"REF-{client_order_id}",
            actual_fill_price=round(actual_fill, 5),
            reject_reason=None,
        )