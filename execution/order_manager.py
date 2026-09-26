"""
execution/order_manager.py
Write-Ahead Order Execution Lifecycle and Pre-Submission Gates.
Implements Master Spec V4.1 Sections 3.3, 6.3, and 6.6.
"""
import json
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from config.enums import (
    AbortReason,
    AuditEventType,
    BlockReason,
    OrderStatus,
    SubAccount,
    TradeDirection,
)


@dataclass(frozen=True)
class OrderExecutionResult:
    client_order_id: str
    order_status: OrderStatus
    abort_reason: Optional[AbortReason] = None
    deal_reference: Optional[str] = None
    deal_id: Optional[str] = None
    actual_fill_price: Optional[float] = None
    error_message: Optional[str] = None


def _now_ms() -> int:
    return int(time.time() * 1000)


def check_pre_submission_gates(
    conn: sqlite3.Connection,
    epic: str,
    broker_positions: List[Any],
) -> Optional[AbortReason]:
    """
    Evaluates Section 3.3 Pre-submission gates inside the order manager:
    1. Active block in epic_blocks?
    2. Foreign unowned position on broker for this epic?
    3. Circuit breaker active?
    """
    # Gate 1: Check epic_blocks
    row_block = conn.execute(
        "SELECT block_id FROM epic_blocks WHERE epic = ? AND unblocked_at IS NULL",
        (epic,),
    ).fetchone()
    if row_block:
        return AbortReason.AMBIGUOUS_BROKER_MATCH

    # Gate 2: Check Circuit Breaker
    row_cb = conn.execute(
        "SELECT value FROM system_state WHERE key = 'circuit_breaker_active'",
    ).fetchone()
    if row_cb and str(row_cb[0]).strip() == "1":
        return AbortReason.CIRCUIT_BREAKER

    # Gate 3: Check Foreign Position on Broker
    # Look for positions on the broker on this epic
    matching_broker_positions = [
        pos for pos in broker_positions
        if getattr(pos, "epic", None) == epic or (isinstance(pos, dict) and pos.get("epic") == epic)
    ]
    if matching_broker_positions:
        # Check if local DB already has an active order/position tracking it
        row_local = conn.execute(
            "SELECT client_order_id FROM orders_and_positions WHERE epic = ? AND order_status IN ('OPEN','CLOSING')",
            (epic,),
        ).fetchone()
        if not row_local:
            # Foreign position exists! Block epic and abort
            now = _now_ms()
            conn.execute(
                "INSERT INTO epic_blocks (epic, reason, blocked_at) VALUES (?, ?, ?)",
                (epic, BlockReason.MANUAL_POSITION_EXISTS.value, now),
            )
            conn.execute(
                """
                INSERT INTO system_audit_log (
                    timestamp, event_type, discrepancy_detected, details_json
                ) VALUES (?, ?, 1, ?)
                """,
                (
                    now,
                    AuditEventType.EPIC_BLOCKED.value,
                    json.dumps({"epic": epic, "reason": "Foreign position found on broker"}),
                ),
            )
            return AbortReason.AMBIGUOUS_BROKER_MATCH

    return None


def execute_order(
    *,
    conn: sqlite3.Connection,
    broker_client: Any,
    client_order_id: str,
    signal_id: Optional[str],
    epic: str,
    sub_account: SubAccount,
    direction: TradeDirection,
    planned_entry_price: float,
    executable_entry_price: float,
    initial_stop_price: float,
    target_price: Optional[float],
    planned_risk_r_usd: float,
    allocated_units: float,
    margin_used_usd: float,
) -> OrderExecutionResult:
    """
    Executes an approved signal candidate through the write-ahead order lifecycle:
    1. Evaluates Pre-submission Gates
    2. Writes QUEUED, then SUBMITTING to SQLite (committed before broker call)
    3. Calls broker POST /api/v1/positions with stop attached
    4. Records dealReference -> transitions to SUBMITTED
    5. Polls confirm -> transitions to OPEN or REJECTED
    On network drop: preserves SUBMITTING / SUBMITTED without false rejection.
    """
    now = _now_ms()

    # Fetch live broker positions for foreign check
    try:
        broker_positions = broker_client.fetch_open_positions()
    except Exception as e:
        # Network drop before write-ahead: abort safely
        return OrderExecutionResult(
            client_order_id=client_order_id,
            order_status=OrderStatus.ABORTED,
            abort_reason=AbortReason.TIMEOUT,
            error_message=f"Failed to fetch broker positions: {e}",
        )

    # 1. Evaluate Pre-submission Gates
    gate_abort = check_pre_submission_gates(conn, epic, broker_positions)
    if gate_abort is not None:
        return OrderExecutionResult(
            client_order_id=client_order_id,
            order_status=OrderStatus.ABORTED,
            abort_reason=gate_abort,
            error_message=f"Pre-submission gate failed: {gate_abort.value}",
        )

    # 2. Write-Ahead: Insert QUEUED -> SUBMITTING
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            """
            INSERT INTO orders_and_positions (
                client_order_id, signal_id, epic, sub_account, direction,
                order_status, planned_entry_price, executable_entry_price,
                initial_stop_price, current_stop_price, target_price,
                planned_risk_r_usd, allocated_units, margin_used_usd,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'QUEUED', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                client_order_id, signal_id, epic, sub_account.value, direction.value,
                planned_entry_price, executable_entry_price, initial_stop_price,
                initial_stop_price, target_price, planned_risk_r_usd, allocated_units,
                margin_used_usd, now, now
            ),
        )
        conn.execute(
            "UPDATE orders_and_positions SET order_status = 'SUBMITTING', updated_at = ? WHERE client_order_id = ?",
            (now, client_order_id),
        )
        conn.commit()
    except Exception as e:
        conn.rollback()
        raise e

    # 3. Network Call: Create Position on Broker
    broker_direction = "BUY" if direction == TradeDirection.BUY else "SELL"
    deal_reference: Optional[str] = None

    try:
        deal_reference = broker_client.create_position(
            epic=epic,
            direction=broker_direction,
            size=allocated_units,
            stop_level=initial_stop_price,
        )
    except Exception as e:
        # TIMEOUT / NETWORK FAILURE on POST:
        # DO NOT mark REJECTED! Row stays in SUBMITTING for Recovery Matrix to inspect.
        err_now = _now_ms()
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            INSERT INTO system_audit_log (
                timestamp, event_type, discrepancy_detected, details_json, client_order_id
            ) VALUES (?, ?, 1, ?, ?)
            """,
            (
                err_now,
                AuditEventType.API_TIMEOUT.value,
                json.dumps({"error": str(e), "stage": "create_position"}),
                client_order_id,
            ),
        )
        conn.commit()

        return OrderExecutionResult(
            client_order_id=client_order_id,
            order_status=OrderStatus.SUBMITTING,
            error_message=f"Network drop during create_position: {type(e).__name__}: {e}",
        )

    # 4. Advance to SUBMITTED with deal_reference
    sub_now = _now_ms()
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        """
        UPDATE orders_and_positions
        SET order_status = 'SUBMITTED', deal_reference = ?, updated_at = ?
        WHERE client_order_id = ?
        """,
        (deal_reference, sub_now, client_order_id),
    )
    conn.commit()

    # 5. Poll Confirmation
    try:
        confirm = broker_client.confirm_order(deal_reference)
    except Exception as e:
        # 504 / NETWORK FAILURE on Confirm endpoint:
        # DO NOT mark REJECTED! Row stays in SUBMITTED with deal_reference preserved.
        conf_err_now = _now_ms()
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            INSERT INTO system_audit_log (
                timestamp, event_type, discrepancy_detected, details_json, client_order_id
            ) VALUES (?, ?, 1, ?, ?)
            """,
            (
                conf_err_now,
                AuditEventType.API_TIMEOUT.value,
                json.dumps({"error": str(e), "stage": "confirm_order", "deal_reference": deal_reference}),
                client_order_id,
            ),
        )
        conn.commit()

        return OrderExecutionResult(
            client_order_id=client_order_id,
            order_status=OrderStatus.SUBMITTED,
            deal_reference=deal_reference,
            error_message=f"Network drop during confirm_order: {type(e).__name__}: {e}",
        )

    # 6. Evaluate Confirmation Result
    confirm_status = getattr(confirm, "status", None) or (confirm.get("status") if isinstance(confirm, dict) else None)
    
    if confirm_status == "ACCEPTED":
        deal_id = getattr(confirm, "deal_id", None) or (confirm.get("deal_id") if isinstance(confirm, dict) else None)
        fill_price = getattr(confirm, "level", None) or (confirm.get("level") if isinstance(confirm, dict) else executable_entry_price)
        open_now = _now_ms()

        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            UPDATE orders_and_positions
            SET order_status = 'OPEN', deal_id = ?, actual_fill_price = ?, opened_at = ?, updated_at = ?
            WHERE client_order_id = ?
            """,
            (deal_id, float(fill_price), open_now, open_now, client_order_id),
        )
        conn.commit()

        return OrderExecutionResult(
            client_order_id=client_order_id,
            order_status=OrderStatus.OPEN,
            deal_reference=deal_reference,
            deal_id=deal_id,
            actual_fill_price=float(fill_price),
        )

    # Broker REJECTED:
    rej_reason = getattr(confirm, "reject_reason", None) or (confirm.get("reject_reason") if isinstance(confirm, dict) else "BROKER_REJECTED")
    rej_now = _now_ms()

    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        """
        UPDATE orders_and_positions
        SET order_status = 'REJECTED', abort_reason = ?, updated_at = ?
        WHERE client_order_id = ?
        """,
        (AbortReason.BROKER_REJECTED.value, rej_now, client_order_id),
    )
    conn.execute(
        """
        INSERT INTO system_audit_log (
            timestamp, event_type, discrepancy_detected, details_json, client_order_id
        ) VALUES (?, ?, 1, ?, ?)
        """,
        (
            rej_now,
            AuditEventType.DISCREPANCY_FOUND.value,
            json.dumps({"reject_reason": rej_reason, "deal_reference": deal_reference}),
            client_order_id,
        ),
    )
    conn.commit()

    return OrderExecutionResult(
        client_order_id=client_order_id,
        order_status=OrderStatus.REJECTED,
        abort_reason=AbortReason.BROKER_REJECTED,
        deal_reference=deal_reference,
        error_message=str(rej_reason),
    )
