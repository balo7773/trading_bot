#!/usr/bin/env python3
"""
tests/test_capital_client.py

Verifies CapitalBrokerClient behavior against simulated HTTP responses:
- Authentication & token extraction
- Two-phase execution (place_order, update_stop, close_position)
- Fetching & mapping open positions
"""
import httpx
import pytest

from execution.capital_client import CapitalBrokerClient


def test_session_authentication_and_token_caching():
    def mock_transport(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/session" and request.method == "POST":
            assert request.headers.get("X-CAP-API-KEY") == "TEST_KEY"
            return httpx.Response(
                200,
                headers={"CST": "MOCK_CST_TOKEN", "X-SECURITY-TOKEN": "MOCK_SEC_TOKEN"},
                json={"currentAccountId": "12345"},
            )
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(mock_transport))
    broker = CapitalBrokerClient(
        api_key="TEST_KEY",
        identifier="test@example.com",
        password="test_password",
        demo=True,
        http_client=client,
    )

    headers = broker._ensure_session()
    assert headers["CST"] == "MOCK_CST_TOKEN"
    assert headers["X-SECURITY-TOKEN"] == "MOCK_SEC_TOKEN"


def test_place_order_two_phase_success():
    def mock_transport(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/session":
            return httpx.Response(
                200,
                headers={"CST": "TOKEN_A", "X-SECURITY-TOKEN": "TOKEN_B"},
                json={},
            )
        if request.url.path == "/api/v1/positions" and request.method == "POST":
            assert request.headers.get("CST") == "TOKEN_A"
            return httpx.Response(
                200,
                json={"dealReference": "o_ref_12345"},
            )
        if request.url.path == "/api/v1/confirms/o_ref_12345":
            return httpx.Response(
                200,
                json={
                    "dealStatus": "ACCEPTED",
                    "dealId": "DEAL_CAPITAL_999",
                    "level": 1.0855,
                },
            )
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(mock_transport))
    broker = CapitalBrokerClient(
        api_key="KEY",
        identifier="user",
        password="pwd",
        demo=True,
        http_client=client,
    )

    result = broker.place_order(
        client_order_id="ORD-CAP-001",
        epic="EURUSD",
        direction="BUY",
        units=1000.0,
        stop_price=1.0810,
        target_price=1.0900,
    )

    assert result.success is True
    assert result.deal_id == "DEAL_CAPITAL_999"
    assert result.deal_reference == "o_ref_12345"
    assert result.actual_fill_price == 1.0855


def test_update_stop_success():
    def mock_transport(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/session":
            return httpx.Response(
                200,
                headers={"CST": "TOKEN_A", "X-SECURITY-TOKEN": "TOKEN_B"},
                json={},
            )
        if request.url.path == "/api/v1/positions/DEAL_TEST_01" and request.method == "PUT":
            return httpx.Response(
                200,
                json={"dealReference": "p_update_ref_01"},
            )
        if request.url.path == "/api/v1/confirms/p_update_ref_01":
            return httpx.Response(
                200,
                json={
                    "dealStatus": "ACCEPTED",
                    "dealId": "DEAL_TEST_01",
                    "level": 1.0870,
                },
            )
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(mock_transport))
    broker = CapitalBrokerClient(
        api_key="KEY",
        identifier="user",
        password="pwd",
        demo=True,
        http_client=client,
    )

    result = broker.update_stop(deal_id="DEAL_TEST_01", new_stop_price=1.0870)
    assert result.success is True
    assert result.deal_id == "DEAL_TEST_01"
    assert result.deal_reference == "p_update_ref_01"


def test_close_position_success():
    def mock_transport(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/session":
            return httpx.Response(
                200,
                headers={"CST": "TOKEN_A", "X-SECURITY-TOKEN": "TOKEN_B"},
                json={},
            )
        if request.url.path == "/api/v1/positions/DEAL_TEST_02" and request.method == "DELETE":
            return httpx.Response(
                200,
                json={"dealReference": "p_close_ref_02"},
            )
        if request.url.path == "/api/v1/confirms/p_close_ref_02":
            return httpx.Response(
                200,
                json={
                    "dealStatus": "ACCEPTED",
                    "dealId": "DEAL_TEST_02",
                    "level": 1.0920,
                },
            )
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(mock_transport))
    broker = CapitalBrokerClient(
        api_key="KEY",
        identifier="user",
        password="pwd",
        demo=True,
        http_client=client,
    )

    result = broker.close_position(deal_id="DEAL_TEST_02")
    assert result.success is True
    assert result.deal_id == "DEAL_TEST_02"
    assert result.actual_fill_price == 1.0920


def test_fetch_open_positions_mapping():
    def mock_transport(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/session":
            return httpx.Response(
                200,
                headers={"CST": "TOKEN_A", "X-SECURITY-TOKEN": "TOKEN_B"},
                json={},
            )
        if request.url.path == "/api/v1/positions" and request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "positions": [
                        {
                            "position": {
                                "dealId": "CAP_DEAL_01",
                                "direction": "BUY",
                                "size": 2500.0,
                                "level": 1.2500,
                            },
                            "market": {"epic": "GBPUSD"},
                        }
                    ]
                },
            )
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(mock_transport))
    broker = CapitalBrokerClient(
        api_key="KEY",
        identifier="user",
        password="pwd",
        demo=True,
        http_client=client,
    )

    positions = broker.fetch_open_positions()
    assert len(positions) == 1
    assert positions[0].deal_id == "CAP_DEAL_01"
    assert positions[0].epic == "GBPUSD"
    assert positions[0].direction == "BUY"
    assert positions[0].units == 2500.0
    assert positions[0].open_level == 1.2500