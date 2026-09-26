"""
monitoring/circuit_breaker.py
Circuit Breakers and Portfolio Drawdown Protection.
Implements Master Spec V4.1 Sections 5.3 and 6.6.
"""
import json
import sqlite3
from dataclasses import dataclass
from typing import Optional

from config.enums import AuditEventType

# Dials per Master Spec Section 5.3
DEFAULT_MAX_DAILY_LOSS_R = 3.0         # -3R daily loss limit
DEFAULT_MAX_ROLLING_LOSS_R = 6.0       # -6R hard master drawdown limit
DEFAULT_MAX_CONSECUTIVE_TIMEOUTS = 3   # 3 consecutive API timeouts trip breaker
TIMEOUT_RECENCY_WINDOW_MS = 3_600_000  # 1 hour recency window for timeouts


@dataclass(frozen=True)
class CircuitBreakerStatus:
    is_active: bool
    tripped_at: Optional[int] = None
    reason: Optional[str] = None


def get_circuit_breaker_status(conn: sqlite3.Connection) -> CircuitBreakerStatus:
    """Reads circuit breaker state atomically in a single query."""
    cursor = conn.cursor()
    rows = cursor.execute(
        """
        SELECT key, value, updated_at FROM system_state
        WHERE key IN ('circuit_breaker_active', 'circuit_breaker_reason')
        """
    ).fetchall()

    data = {row[0]: (row[1], row[2]) for row in rows}

    active_val, active_time = data.get("circuit_breaker_active", ("0", None))
    reason_val, _ = data.get("circuit_breaker_reason", ("NONE", None))

    is_active = str(active_val).strip() == "1"
    tripped_at = int(active_time) if active_time else None
    reason = str(reason_val) if is_active else None

    return CircuitBreakerStatus(is_active=is_active, tripped_at=tripped_at, reason=reason)


def trip_circuit_breaker(
    conn: sqlite3.Connection,
    reason: str,
    current_time_ms: int,
    details: Optional[dict] = None,
) -> None:
    """Trips the circuit breaker under BEGIN IMMEDIATE."""
    details_json = json.dumps(details or {"reason": reason})

    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        """
        INSERT INTO system_state (key, value, updated_at)
        VALUES ('circuit_breaker_active', '1', ?)
        ON CONFLICT(key) DO UPDATE SET value = '1', updated_at = excluded.updated_at
        """,
        (current_time_ms,),
    )
    conn.execute(
        """
        INSERT INTO system_state (key, value, updated_at)
        VALUES ('circuit_breaker_reason', ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        (reason, current_time_ms),
    )
    conn.execute(
        """
        INSERT INTO system_audit_log (
            timestamp, event_type, discrepancy_detected, details_json
        ) VALUES (?, ?, 1, ?)
        """,
        (current_time_ms, AuditEventType.CIRCUIT_BREAKER_TRIP.value, details_json),
    )
    conn.commit()


def reset_circuit_breaker(
    conn: sqlite3.Connection,
    current_time_ms: int,
    force: bool = False,
    rolling_drawdown_r: Optional[float] = None,
) -> bool:
    """
    Resets the circuit breaker:
    - Rejects reset if rolling drawdown is actively <= -6.0R (unless force=True).
    - If tripped by ROLLING_DRAWDOWN_LIMIT or CONSECUTIVE_TIMEOUTS, requires force=True.
    - Soft daily trip (-3R) resets cleanly if rolling drawdown is healthy.
    """
    status = get_circuit_breaker_status(conn)
    if not status.is_active:
        return True

    # Guard: Hard rolling drawdown check
    if not force and rolling_drawdown_r is not None and rolling_drawdown_r <= -DEFAULT_MAX_ROLLING_LOSS_R:
        return False

    if not force and status.reason in ("ROLLING_DRAWDOWN_LIMIT", "CONSECUTIVE_TIMEOUTS"):
        return False

    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        """
        INSERT INTO system_state (key, value, updated_at)
        VALUES ('circuit_breaker_active', '0', ?)
        ON CONFLICT(key) DO UPDATE SET value = '0', updated_at = excluded.updated_at
        """,
        (current_time_ms,),
    )
    conn.execute(
        """
        INSERT INTO system_state (key, value, updated_at)
        VALUES ('circuit_breaker_reason', 'NONE', ?)
        ON CONFLICT(key) DO UPDATE SET value = 'NONE', updated_at = excluded.updated_at
        """,
        (current_time_ms,),
    )
    conn.execute(
        """
        INSERT INTO system_audit_log (
            timestamp, event_type, discrepancy_detected, details_json
        ) VALUES (?, ?, 0, ?)
        """,
        (
            current_time_ms,
            AuditEventType.DISCREPANCY_FOUND.value,
            json.dumps({"action": "Circuit breaker reset", "force": force, "previous_reason": status.reason}),
        ),
    )
    conn.commit()
    return True


def audit_session_drawdown(
    *,
    conn: sqlite3.Connection,
    current_time_ms: int,
    session_pnl_usd: float,
    r_unit_usd: float,
    max_daily_loss_r: float = DEFAULT_MAX_DAILY_LOSS_R,
    rolling_drawdown_r: Optional[float] = None,
    max_rolling_loss_r: float = DEFAULT_MAX_ROLLING_LOSS_R,
) -> bool:
    """Evaluates session and rolling loss against 1R dials."""
    if r_unit_usd <= 0:
        raise ValueError("r_unit_usd must be strictly positive.")

    # 1. Hard Rolling Drawdown Check
    if rolling_drawdown_r is not None and rolling_drawdown_r <= -max_rolling_loss_r:
        trip_circuit_breaker(
            conn=conn,
            reason="ROLLING_DRAWDOWN_LIMIT",
            current_time_ms=current_time_ms,
            details={"rolling_drawdown_r": rolling_drawdown_r, "threshold_r": -max_rolling_loss_r},
        )
        return True

    # 2. Soft Daily Drawdown Check
    loss_r = session_pnl_usd / r_unit_usd
    if loss_r <= -max_daily_loss_r:
        trip_circuit_breaker(
            conn=conn,
            reason="DAILY_DRAWDOWN_LIMIT",
            current_time_ms=current_time_ms,
            details={"session_loss_r": round(loss_r, 4), "threshold_r": -max_daily_loss_r},
        )
        return True

    return False


def audit_consecutive_timeouts(
    *,
    conn: sqlite3.Connection,
    current_time_ms: int,
    max_timeouts: int = DEFAULT_MAX_CONSECUTIVE_TIMEOUTS,
    recency_window_ms: int = TIMEOUT_RECENCY_WINDOW_MS,
) -> bool:
    """
    Checks if >= max_timeouts consecutive API_TIMEOUT events occurred within
    the recency window without an intervening successful operation.
    """
    cursor = conn.cursor()
    cutoff_time = current_time_ms - recency_window_ms

    rows = cursor.execute(
        """
        SELECT event_type FROM system_audit_log
        WHERE timestamp >= ?
        ORDER BY timestamp DESC, log_id DESC
        LIMIT ?
        """,
        (cutoff_time, max_timeouts),
    ).fetchall()

    if len(rows) < max_timeouts:
        return False

    if all(row[0] == AuditEventType.API_TIMEOUT.value for row in rows):
        trip_circuit_breaker(
            conn=conn,
            reason="CONSECUTIVE_TIMEOUTS",
            current_time_ms=current_time_ms,
            details={"consecutive_timeouts": max_timeouts, "within_window_ms": recency_window_ms},
        )
        return True

    return False
