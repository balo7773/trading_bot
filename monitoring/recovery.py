"""
monitoring/recovery.py
Startup Recovery Matrix for Crash & Reconnection Resilience.
Implements Master Spec V4.1 Section 6.4.
"""
import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from config.enums import (
    AbortReason,
    AuditEventType,
    BlockReason,
    OrderStatus,
    TradeDirection,
)

# Constants
RECOVERY_GRACE_WINDOW_MS = 600_000  # 10 minutes: minimum age to declare abandoned


@dataclass
class StartupRecoveryReport:
    submitted_resolved_open: List[str] = field(default_factory=list)
    submitted_resolved_rejected: List[str] = field(default_factory=list)
    submitting_adopted_open: List[str] = field(default_factory=list)
    submitting_abandoned_aborted: List[str] = field(default_factory=list)
    queued_cancelled: List[str] = field(default_factory=list)
    ambiguous_escalated: List[str] = field(default_factory=list)


def _get_attr(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def run_startup_recovery(
    *,
    conn: sqlite3.Connection,
    broker_client: Any,
    current_time_ms: int,
) -> StartupRecoveryReport:
    """
    Executes the Section 6.4 Startup Recovery Matrix in strict order:
    1. Resolve SUBMITTED rows (via confirm_order or /positions fallback)
    2. Resolve SUBMITTING rows (via composite /positions attribute matching)
    3. Clean up QUEUED rows (safe transition to ABORTED)
    """
    report = StartupRecoveryReport()

    # Fetch live broker positions once for fallbacks and attribute matching
    broker_positions = broker_client.fetch_open_positions()
    broker_by_epic: Dict[str, List[Any]] = {}
    for pos in broker_positions:
        epic = _get_attr(pos, "epic")
        if epic:
            broker_by_epic.setdefault(epic, []).append(pos)

    # -------------------------------------------------------------------------
    # 1. RESOLVE SUBMITTED ROWS (Have deal_reference)
    # -------------------------------------------------------------------------
    submitted_rows = conn.execute(
        """
        SELECT client_order_id, epic, direction, deal_reference,
               allocated_units, executable_entry_price, updated_at
        FROM orders_and_positions
        WHERE order_status = 'SUBMITTED'
        """
    ).fetchall()

    for row in submitted_rows:
        c_order_id, epic, dir_str, deal_ref, units, exec_price, updated_at = row
        direction = TradeDirection(dir_str)
        resolved = False

        # Attempt primary confirmation poll
        try:
            confirm = broker_client.confirm_order(deal_ref)
            status = _get_attr(confirm, "status")
            if status == "ACCEPTED":
                deal_id = _get_attr(confirm, "deal_id")
                fill_price = float(_get_attr(confirm, "level", exec_price))

                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    """
                    UPDATE orders_and_positions
                    SET order_status = 'OPEN', deal_id = ?, actual_fill_price = ?,
                        opened_at = ?, trailing_high_watermark = ?, trailing_low_watermark = ?,
                        updated_at = ?
                    WHERE client_order_id = ?
                    """,
                    (deal_id, fill_price, current_time_ms, fill_price, fill_price, current_time_ms, c_order_id),
                )
                conn.execute(
                    """
                    INSERT INTO system_audit_log (
                        timestamp, event_type, discrepancy_detected, details_json, client_order_id
                    ) VALUES (?, ?, 1, ?, ?)
                    """,
                    (
                        current_time_ms,
                        AuditEventType.DISCREPANCY_FOUND.value,
                        json.dumps({"recovery": "SUBMITTED confirmed ACCEPTED", "deal_id": deal_id}),
                        c_order_id,
                    ),
                )
                conn.commit()
                report.submitted_resolved_open.append(c_order_id)
                resolved = True

            elif status == "REJECTED":
                rej_reason = _get_attr(confirm, "reject_reason", "BROKER_REJECTED")
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    """
                    UPDATE orders_and_positions
                    SET order_status = 'REJECTED', abort_reason = ?, updated_at = ?
                    WHERE client_order_id = ?
                    """,
                    (AbortReason.BROKER_REJECTED.value, current_time_ms, c_order_id),
                )
                conn.execute(
                    """
                    INSERT INTO system_audit_log (
                        timestamp, event_type, discrepancy_detected, details_json, client_order_id
                    ) VALUES (?, ?, 1, ?, ?)
                    """,
                    (
                        current_time_ms,
                        AuditEventType.DISCREPANCY_FOUND.value,
                        json.dumps({"recovery": "SUBMITTED confirmed REJECTED", "reason": rej_reason}),
                        c_order_id,
                    ),
                )
                conn.commit()
                report.submitted_resolved_rejected.append(c_order_id)
                resolved = True

        except Exception:
            resolved = False

        # Fallback to /positions if dealReference is expired or 404
        if not resolved:
            b_positions = broker_by_epic.get(epic, [])
            matched = False
            for b_pos in b_positions:
                b_dir = TradeDirection.BUY if _get_attr(b_pos, "direction") == "BUY" else TradeDirection.SELL_SHORT
                b_size = float(_get_attr(b_pos, "size", 0.0))
                if b_dir == direction and abs(b_size - units) < 1e-4:
                    deal_id = _get_attr(b_pos, "deal_id")
                    fill_price = float(_get_attr(b_pos, "level", exec_price))
                    conn.execute("BEGIN IMMEDIATE")
                    conn.execute(
                        """
                        UPDATE orders_and_positions
                        SET order_status = 'OPEN', deal_id = ?, actual_fill_price = ?,
                            opened_at = ?, trailing_high_watermark = ?, trailing_low_watermark = ?,
                            updated_at = ?
                        WHERE client_order_id = ?
                        """,
                        (deal_id, fill_price, current_time_ms, fill_price, fill_price, current_time_ms, c_order_id),
                    )
                    conn.commit()
                    report.submitted_resolved_open.append(c_order_id)
                    matched = True
                    break

            if not matched and (current_time_ms - updated_at) >= RECOVERY_GRACE_WINDOW_MS:
                # No broker position found after grace window -> Mark REJECTED
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    """
                    UPDATE orders_and_positions
                    SET order_status = 'REJECTED', abort_reason = ?, updated_at = ?
                    WHERE client_order_id = ?
                    """,
                    (AbortReason.BROKER_REJECTED.value, current_time_ms, c_order_id),
                )
                conn.commit()
                report.submitted_resolved_rejected.append(c_order_id)

    # -------------------------------------------------------------------------
    # 2. RESOLVE SUBMITTING ROWS (No deal_reference)
    # -------------------------------------------------------------------------
    submitting_rows = conn.execute(
        """
        SELECT client_order_id, epic, direction, allocated_units,
               executable_entry_price, updated_at
        FROM orders_and_positions
        WHERE order_status = 'SUBMITTING'
        """
    ).fetchall()

    for row in submitting_rows:
        c_order_id, epic, dir_str, units, exec_price, updated_at = row
        direction = TradeDirection(dir_str)
        b_positions = broker_by_epic.get(epic, [])
        adopted = False

        for b_pos in b_positions:
            b_dir = TradeDirection.BUY if _get_attr(b_pos, "direction") == "BUY" else TradeDirection.SELL_SHORT
            b_size = float(_get_attr(b_pos, "size", 0.0))

            if b_dir == direction and abs(b_size - units) < 1e-4:
                deal_id = _get_attr(b_pos, "deal_id")
                fill_price = float(_get_attr(b_pos, "level", exec_price))
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    """
                    UPDATE orders_and_positions
                    SET order_status = 'OPEN', deal_id = ?, actual_fill_price = ?,
                        opened_at = ?, trailing_high_watermark = ?, trailing_low_watermark = ?,
                        updated_at = ?
                    WHERE client_order_id = ?
                    """,
                    (deal_id, fill_price, current_time_ms, fill_price, fill_price, current_time_ms, c_order_id),
                )
                conn.execute(
                    """
                    INSERT INTO system_audit_log (
                        timestamp, event_type, discrepancy_detected, details_json, client_order_id
                    ) VALUES (?, ?, 1, ?, ?)
                    """,
                    (
                        current_time_ms,
                        AuditEventType.DISCREPANCY_FOUND.value,
                        json.dumps({"recovery": "SUBMITTING matched and adopted to OPEN", "deal_id": deal_id}),
                        c_order_id,
                    ),
                )
                conn.commit()
                report.submitting_adopted_open.append(c_order_id)
                adopted = True
                break

        if not adopted:
            if (current_time_ms - updated_at) >= RECOVERY_GRACE_WINDOW_MS:
                # Execution window expired and broker has no record -> Safe to abort
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    """
                    UPDATE orders_and_positions
                    SET order_status = 'ABORTED', abort_reason = ?, updated_at = ?
                    WHERE client_order_id = ?
                    """,
                    (AbortReason.TIMEOUT.value, current_time_ms, c_order_id),
                )
                conn.commit()
                report.submitting_abandoned_aborted.append(c_order_id)
            else:
                report.ambiguous_escalated.append(c_order_id)

    # -------------------------------------------------------------------------
    # 3. CLEAN UP QUEUED ROWS (Never reached broker network layer)
    # -------------------------------------------------------------------------
    queued_rows = conn.execute(
        "SELECT client_order_id FROM orders_and_positions WHERE order_status = 'QUEUED'"
    ).fetchall()

    for (c_order_id,) in queued_rows:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            UPDATE orders_and_positions
            SET order_status = 'ABORTED', abort_reason = ?, updated_at = ?
            WHERE client_order_id = ?
            """,
            (AbortReason.TIMEOUT.value, current_time_ms, c_order_id),
        )
        conn.commit()
        report.queued_cancelled.append(c_order_id)

    return report
