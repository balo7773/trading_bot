#!/usr/bin/env python3
"""
database/connection.py

Manages SQLite connection lifecycle, production PRAGMAs, and schema initialization.
"""
from pathlib import Path
import sqlite3
from typing import Generator
from contextlib import contextmanager

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = PROJECT_ROOT / "database" / "schema.sql"
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "trading_bot.db"


def init_db(db_path: Path = DEFAULT_DB_PATH) -> None:
    """Creates directory if needed and applies schema.sql on an uninitialized DB."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with get_connection(db_path) as conn:
        schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")
        conn.executescript(schema_sql)


def configure_connection(conn: sqlite3.Connection) -> sqlite3.Connection:
    """Applies production PRAGMAs and row formatting to an active SQLite connection."""
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("PRAGMA foreign_keys = ON;")
    cursor.execute("PRAGMA journal_mode = WAL;")
    cursor.execute("PRAGMA synchronous = NORMAL;")
    cursor.execute("PRAGMA busy_timeout = 5000;")
    cursor.close()
    return conn


@contextmanager
def get_connection(db_path: Path = DEFAULT_DB_PATH) -> Generator[sqlite3.Connection, None, None]:
    """
    Context manager yielding a properly configured SQLite connection.
    Handles commit on success and rollback on unhandled exceptions.
    """
    conn = sqlite3.connect(str(db_path), timeout=5.0)
    configure_connection(conn)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()