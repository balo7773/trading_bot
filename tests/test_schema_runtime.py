#!/usr/bin/env python3
"""
tests/test_schema_runtime.py

Verifies that SQLite actively enforces CHECK constraints, nullable enums,
and compound integrity rules defined in database/schema.sql at runtime.
"""
from pathlib import Path
import sqlite3
import pytest

SCHEMA_PATH = Path(__file__).parent.parent / "database" / "schema.sql"


@pytest.fixture
def db_conn():
    """Provides an isolated, in-memory SQLite database initialized with our schema."""
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON;")
    
    schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")
    conn.executescript(schema_sql)
    
    yield conn
    conn.close()


def _base_order_payload(**overrides):
    """Helper returning a valid baseline record for orders_and_positions."""
    data = {
        "client_order_id": "ORD-001",
        "epic": "CS.D.EURUSD.CFD.IP",
        "sub_account": "SUB_A_MR",
        "direction": "BUY",
        "order_status": "QUEUED",
        "planned_entry_price": 1.0850,
        "initial_stop_price": 1.0800,
        "current_stop_price": 1.0800,
        "planned_risk_r_usd": 100.0,
        "allocated_units": 10000.0,
        "abort_reason": None,
        "close_reason": None,
        "created_at": 1700000000000,
        "updated_at": 1700000000000,
    }
    data.update(overrides)
    return data


def _insert_order(conn, payload):
    keys = ", ".join(payload.keys())
    placeholders = ", ".join(f":{k}" for k in payload.keys())
    query = f"INSERT INTO orders_and_positions ({keys}) VALUES ({placeholders});"
    conn.execute(query, payload)


def test_valid_order_insert_succeeds(db_conn):
    """A valid order with NULL abort_reason and valid enums must insert cleanly."""
    payload = _base_order_payload(client_order_id="ORD-VALID-01")
    _insert_order(db_conn, payload)
    db_conn.commit()

    cursor = db_conn.cursor()
    cursor.execute(
        "SELECT client_order_id, order_status, abort_reason FROM orders_and_positions WHERE client_order_id = ?",
        ("ORD-VALID-01",),
    )
    row = cursor.fetchone()
    assert row == ("ORD-VALID-01", "QUEUED", None)


def test_invalid_enum_rejected(db_conn):
    """Providing an invalid direction (e.g., 'LONG' instead of 'BUY') violates CHECK."""
    payload = _base_order_payload(client_order_id="ORD-INVALID-DIR", direction="LONG")
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        _insert_order(db_conn, payload)


def test_invalid_abort_reason_rejected(db_conn):
    """An unlisted abort_reason string must be rejected even though the field is nullable."""
    payload = _base_order_payload(
        client_order_id="ORD-INVALID-ABORT",
        abort_reason="SYSTEM_GLITCH",
    )
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        _insert_order(db_conn, payload)


def test_terminal_state_requires_abort_reason(db_conn):
    """
    Validates chk_terminal_has_reason:
    Status ABORTED or REJECTED requires abort_reason IS NOT NULL.
    """
    # 1. ABORTED with NULL abort_reason -> Must fail
    bad_payload = _base_order_payload(
        client_order_id="ORD-ABORT-FAIL",
        order_status="ABORTED",
        abort_reason=None,
    )
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        _insert_order(db_conn, bad_payload)

    # 2. ABORTED with valid abort_reason -> Must succeed
    good_payload = _base_order_payload(
        client_order_id="ORD-ABORT-PASS",
        order_status="ABORTED",
        abort_reason="SPREAD_GATE_EXCEEDED",
    )
    _insert_order(db_conn, good_payload)
    db_conn.commit()