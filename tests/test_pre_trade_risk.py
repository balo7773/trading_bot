#!/usr/bin/env python3
"""
tests/test_pre_trade_risk.py

Verifies position sizing mechanics and sequential pre-trade safety gates.
"""
import pytest
from database.connection import get_connection, init_db
from database.repository import block_epic
from risk.pre_trade import calculate_position_size, evaluate_pre_trade_risk


def test_position_sizing_calculation():
    """Validates inverse scaling of units against stop distance."""
    # 100 USD risk with 0.0020 (20 pips) stop distance -> 50,000 units
    units = calculate_position_size(
        risk_budget_usd=100.0,
        stop_distance=0.0020,
        point_value_per_unit=1.0,
    )
    assert units == 50000.0

    # Halving the stop distance to 0.0010 must double the units to 100,000
    units_tight_stop = calculate_position_size(
        risk_budget_usd=100.0,
        stop_distance=0.0010,
        point_value_per_unit=1.0,
    )
    assert units_tight_stop == 100000.0


def test_position_sizing_rejects_non_positive_values():
    """Ensures division by zero or negative stop distance raises ValueError."""
    with pytest.raises(ValueError, match="stop_distance must be strictly positive"):
        calculate_position_size(risk_budget_usd=100.0, stop_distance=0.0)

    with pytest.raises(ValueError, match="risk_budget_usd must be strictly positive"):
        calculate_position_size(risk_budget_usd=0.0, stop_distance=0.0020)


def test_gate_rejects_blocked_epic(tmp_path):
    """Signals on blocked epics must be rejected with CIRCUIT_BREAKER."""
    db_file = tmp_path / "risk_test_blocked.db"
    init_db(db_file)
    epic = "CS.D.EURUSD.CFD.IP"

    with get_connection(db_file) as conn:
        block_epic(conn, epic=epic, reason="PERSISTENT_DESYNC")

        result = evaluate_pre_trade_risk(
            conn,
            epic=epic,
            current_spread=0.8,
            max_allowed_spread=1.5,
            risk_budget_usd=50.0,
            stop_distance=0.0025,
            target_distance=0.0050,
        )

        assert result.approved is False
        assert result.abort_reason == "CIRCUIT_BREAKER"
        assert result.allocated_units == 0.0


def test_gate_rejects_excessive_spread(tmp_path):
    """Signals where spread exceeds limit must be rejected with SPREAD_GATE_EXCEEDED."""
    db_file = tmp_path / "risk_test_spread.db"
    init_db(db_file)
    epic = "CS.D.GBPUSD.CFD.IP"

    with get_connection(db_file) as conn:
        result = evaluate_pre_trade_risk(
            conn,
            epic=epic,
            current_spread=2.5,
            max_allowed_spread=1.8,
            risk_budget_usd=50.0,
            stop_distance=0.0030,
            target_distance=0.0060,
        )

        assert result.approved is False
        assert result.abort_reason == "SPREAD_GATE_EXCEEDED"


def test_gate_rejects_insufficient_rr_ratio(tmp_path):
    """Signals with reward-to-risk below floor must be rejected with RR_BELOW_FLOOR."""
    db_file = tmp_path / "risk_test_rr.db"
    init_db(db_file)
    epic = "CS.D.USDJPY.CFD.IP"

    with get_connection(db_file) as conn:
        # Target = 0.0020, Stop Distance = 0.0020 -> R:R = 1.0 (below min floor of 1.5)
        result = evaluate_pre_trade_risk(
            conn,
            epic=epic,
            current_spread=0.7,
            max_allowed_spread=1.5,
            risk_budget_usd=50.0,
            stop_distance=0.0020,
            target_distance=0.0020,
            min_rr_floor=1.5,
        )

        assert result.approved is False
        assert result.abort_reason == "RR_BELOW_FLOOR"


def test_gate_approves_valid_signal(tmp_path):
    """A signal satisfying all gates must be approved with computed unit sizing."""
    db_file = tmp_path / "risk_test_approved.db"
    init_db(db_file)
    epic = "CS.D.EURUSD.CFD.IP"

    with get_connection(db_file) as conn:
        result = evaluate_pre_trade_risk(
            conn,
            epic=epic,
            current_spread=0.9,
            max_allowed_spread=1.5,
            risk_budget_usd=50.0,
            stop_distance=0.0020,
            target_distance=0.0040,  # R:R = 2.0 (>= 1.5)
            point_value_per_unit=1.0,
        )

        assert result.approved is True
        assert result.abort_reason is None
        assert result.planned_risk_r_usd == 50.0
        assert result.allocated_units == 25000.0  # 50 / 0.0020