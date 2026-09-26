#!/usr/bin/env python3
"""
tests/test_connection.py

Validates connection factory PRAGMAs, transaction rollback, and row access.
"""
import sqlite3
import pytest
from database.connection import get_connection, init_db


def test_pragmas_applied(tmp_path):
    db_file = tmp_path / "test.db"
    with get_connection(db_file) as conn:
        # Check foreign_keys = ON (1)
        fk_status = conn.execute("PRAGMA foreign_keys;").fetchone()[0]
        assert fk_status == 1

        # Check journal_mode = wal
        journal_mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]
        assert journal_mode.lower() == "wal"

        # Check busy_timeout = 5000
        busy_timeout = conn.execute("PRAGMA busy_timeout;").fetchone()[0]
        assert busy_timeout == 5000


def test_transaction_rollback_on_error(tmp_path):
    db_file = tmp_path / "test.db"
    init_db(db_file)

    with pytest.raises(ValueError):
        with get_connection(db_file) as conn:
            conn.execute(
                """
                INSERT INTO epic_blocks (epic, reason, blocked_at)
                VALUES ('TEST.EPIC', 'PERSISTENT_DESYNC', 1700000000000);
                """
            )
            # Simulated pipeline crash after insert, before commit:
            raise ValueError("Simulated pipeline crash before commit")

    # Verify record was rolled back by the context manager's except block:
    with get_connection(db_file) as conn:
        row = conn.execute("SELECT * FROM epic_blocks WHERE epic = 'TEST.EPIC';").fetchone()
        assert row is None