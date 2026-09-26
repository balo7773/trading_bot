#!/usr/bin/env python3
"""
tests/test_enum_ddl_parity.py

Verifies that database/schema.sql matches config/enums.py exactly.
Fails if any enum value is missing or extra in the database schema.
"""
import re
from pathlib import Path
import pytest
from config.enums import (
    EngineType,
    PermittedDirection,
    TradeDirection,
    SubAccount,
    SignalStatus,
    OrderStatus,
    AbortReason,
    CloseReason,
    AuditEventType,
    BlockReason,
)

SCHEMA_PATH = Path(__file__).parent.parent / "database" / "schema.sql"


def extract_check_values(sql_text: str, column_name: str) -> set[str]:
    """Finds CHECK (column_name IN (...)) and returns the values as a set."""
    pattern = rf"CHECK\s*\(\s*(?:{column_name}\s+IS\s+NULL\s+OR\s+)?{column_name}\s+IN\s*\(([^)]+)\)\)"
    match = re.search(pattern, sql_text, re.IGNORECASE)
    assert match, f"Could not find CHECK constraint for column: {column_name}"
    
    raw_list = match.group(1)
    # Strip whitespace, quotes, and split into set
    return {val.strip().strip("'\"") for val in raw_list.split(",") if val.strip()}


# Mapping of (Column Name, Enum Class)
PARITY_MAP = [
    ("assigned_engine", EngineType),
    ("permitted_direction", PermittedDirection),
    ("direction", TradeDirection),
    ("sub_account", SubAccount),
    ("signal_status", SignalStatus),
    ("order_status", OrderStatus),
    ("abort_reason", AbortReason),
    ("close_reason", CloseReason),
    ("event_type", AuditEventType),
    ("reason", BlockReason),
]


@pytest.mark.parametrize("column_name,enum_cls", PARITY_MAP)
def test_enum_to_ddl_parity(column_name, enum_cls):
    """Guarantees every Python enum matches its SQL CHECK constraint exactly."""
    sql_text = SCHEMA_PATH.read_text(encoding="utf-8")
    
    sql_values = extract_check_values(sql_text, column_name)
    enum_values = {item.value for item in enum_cls}
    
    missing_in_sql = enum_values - sql_values
    extra_in_sql = sql_values - enum_values
    
    error_msg = []
    if missing_in_sql:
        error_msg.append(f"Missing in schema.sql: {missing_in_sql}")
    if extra_in_sql:
        error_msg.append(f"Extra in schema.sql: {extra_in_sql}")
        
    assert not error_msg, (
        f"Parity mismatch for '{column_name}'!\n"
        + "\n".join(error_msg)
        + "\nRun 'python3 -m database.schema_builder' to regenerate schema.sql."
    )