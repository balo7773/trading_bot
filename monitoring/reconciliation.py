"""
monitoring/reconciliation.py
15-Minute Position & Stop Reconciliation Engine.
Implements Master Spec V4.1 Section 6.5.
"""
import json
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from config.enums import (
    AuditEventType,
    BlockReason,
    CloseReason,
    OrderStatus,
    TradeDirection,
)

# Constants per Master Spec Section 6.5
IN_FLIGHT_MUTATION_WINDOW_MS = 30_000    # 30 seconds: skip stop repair if mutated recently
GHOST_EXIT_GRACE_WINDOW_MS = 300_000     # 5 minutes: grace window for eventual consistency


@dataclass
class ReconciliationReport:
    ghost_exits_closed: List[str] = field(default_factory=list)
    stops_repaired: List[str] = field(default_factory=list)
    stops_adopted: List[str] = field(default_factory=list)
    emergency_stops_injected: List[str] = field(default_factory=list)
    orphans_adopted: List[str] = field(default_factory=list)
    blocked_foreign_epics: List[str] = field(default_factory=list)
    partial_closes_synced: List[str] = field(default_factory=list)


def _get_attr(obj: Any, key: str, default: Any = None) -> Any:
    """Helper to access dict keys or object attributes transparently."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def reconcile_positions(
    *,
    conn: sqlite3.Connection,
    broker_client: Any,
    current_time_ms: int,
    tick_size_map: Optional[Dict[str, float]] = None,
) -> ReconciliationReport:
    """
    Executes the 15-minute defensive audit against Capital.com:
    1. Single GET /positions snapshot
    2. Ghost Exit detection (Local OPEN -> Broker Missing)
    3. Stop level discrepancy audit (Direction-aware repair / adopt / emergency injection)
    4. Partial close scaling
    5. Late-fill orphan adoption (SUBMITTING / SUBMITTED -> OPEN)
    6. Foreign manual position containment (epic_blocks)
    """
    report = ReconciliationReport()
    tick_sizes = tick_size_map or {}

    # 1. Single Snapshot from Broker
    broker_positions = broker_client.fetch_open_positions()

    # Index broker positions by epic
    broker_by_epic: Dict[str, List[Any]] = {}
    for pos in broker_positions:
        epic = _get_attr(pos, "epic")
        if epic:
            broker_by_epic.setdefault(epic, []).append(pos)

    # 2. Query Local State
    cursor = conn.cursor()
    local_rows = cursor.execute(
        """
        SELECT client_order_id, signal_id, epic, direction, order_status,
               allocated_units, actual_fill_price, current_stop_price, stop_version,
               target_price, margin_used_usd, opened_at, updated_at
        FROM orders_and_positions
        WHERE order_status IN ('OPEN', 'CLOSING', 'SUBMITTING', 'SUBMITTED')
        """
    ).fetchall()

    matched_broker_epics = set()

    # -------------------------------------------------------------------------
    # PASS 1: Audit Local OPEN / CLOSING Positions
    # -------------------------------------------------------------------------
    for row in local_rows:
        (
            c_order_id, sig_id, epic, direction_str, status_str,
            units, fill_price, curr_stop, stop_ver, target_price,
            margin_used, opened_at, updated_at
        ) = row

        direction = TradeDirection(direction_str)
        status = OrderStatus(status_str)
        tick_size = tick_sizes.get(epic, 0.01)

        b_positions = broker_by_epic.get(epic, [])

        if status == OrderStatus.OPEN:
            if not b_positions:
                # Ghost Exit Check
                time_since_open = current_time_ms - (opened_at or updated_at)
                if time_since_open < GHOST_EXIT_GRACE_WINDOW_MS:
                    # Within 5-minute eventual consistency window: skip
                    continue

                # Definite Ghost Exit: Broker executed stop or target
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    """
                    UPDATE orders_and_positions
                    SET order_status = 'CLOSED', closed_at = ?, close_reason = ?, updated_at = ?
                    WHERE client_order_id = ?
                    """,
                    (current_time_ms, CloseReason.STOP_BROKER.value, current_time_ms, c_order_id),
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
                        json.dumps({"issue": "Ghost Exit detected. Closed locally.", "epic": epic}),
                        c_order_id,
                    ),
                )
                conn.commit()
                report.ghost_exits_closed.append(c_order_id)
                continue

            # Position exists on both Local and Broker
            matched_broker_epics.add(epic)
            b_pos = b_positions[0]
            b_stop = _get_attr(b_pos, "stop_level")
            b_size = float(_get_attr(b_pos, "size", units))
            deal_id = _get_attr(b_pos, "deal_id")

            # Check In-Flight Mutation Guard (30-second cooldown)
            time_since_update = current_time_ms - updated_at
            if time_since_update < IN_FLIGHT_MUTATION_WINDOW_MS:
                continue

            # Check Partial Closes
            if units - b_size > 1e-4:
                scale = b_size / units
                new_margin = round(margin_used * scale, 4)
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    """
                    UPDATE orders_and_positions
                    SET allocated_units = ?, margin_used_usd = ?, updated_at = ?
                    WHERE client_order_id = ?
                    """,
                    (b_size, new_margin, current_time_ms, c_order_id),
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
                        json.dumps({"issue": "Partial close synced", "old_units": units, "new_units": b_size}),
                        c_order_id,
                    ),
                )
                conn.commit()
                report.partial_closes_synced.append(c_order_id)

            # Check Stop Levels (Direction-Aware)
            if b_stop is None:
                # P0: Emergency Stop Injection
                broker_client.update_position(deal_id=deal_id, stop_level=curr_stop)
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    """
                    INSERT INTO system_audit_log (
                        timestamp, event_type, discrepancy_detected, details_json, client_order_id
                    ) VALUES (?, ?, 1, ?, ?)
                    """,
                    (
                        current_time_ms,
                        AuditEventType.DISCREPANCY_FOUND.value,
                        json.dumps({"issue": "Emergency stop injection", "injected_stop": curr_stop}),
                        c_order_id,
                    ),
                )
                conn.commit()
                report.emergency_stops_injected.append(c_order_id)

            else:
                b_stop = float(b_stop)
                if direction == TradeDirection.BUY:
                    if b_stop < curr_stop - tick_size:
                        # Looser -> Re-tighten broker
                        broker_client.update_position(deal_id=deal_id, stop_level=curr_stop)
                        conn.execute("BEGIN IMMEDIATE")
                        conn.execute(
                            """
                            INSERT INTO system_audit_log (
                                timestamp, event_type, discrepancy_detected, details_json, client_order_id
                            ) VALUES (?, ?, 1, ?, ?)
                            """,
                            (
                                current_time_ms,
                                AuditEventType.DISCREPANCY_FOUND.value,
                                json.dumps({"action": "Re-tightened looser stop", "broker": b_stop, "local": curr_stop}),
                                c_order_id,
                            ),
                        )
                        conn.commit()
                        report.stops_repaired.append(c_order_id)

                    elif b_stop > curr_stop + tick_size:
                        # Tighter -> Adopt locally
                        conn.execute("BEGIN IMMEDIATE")
                        conn.execute(
                            """
                            UPDATE orders_and_positions
                            SET current_stop_price = ?, stop_version = stop_version + 1, updated_at = ?
                            WHERE client_order_id = ?
                            """,
                            (b_stop, current_time_ms, c_order_id),
                        )
                        conn.commit()
                        report.stops_adopted.append(c_order_id)

                else:  # SELL_SHORT
                    if b_stop > curr_stop + tick_size:
                        # Looser -> Re-tighten broker
                        broker_client.update_position(deal_id=deal_id, stop_level=curr_stop)
                        conn.execute("BEGIN IMMEDIATE")
                        conn.execute(
                            """
                            INSERT INTO system_audit_log (
                                timestamp, event_type, discrepancy_detected, details_json, client_order_id
                            ) VALUES (?, ?, 1, ?, ?)
                            """,
                            (
                                current_time_ms,
                                AuditEventType.DISCREPANCY_FOUND.value,
                                json.dumps({"action": "Re-tightened looser stop", "broker": b_stop, "local": curr_stop}),
                                c_order_id,
                            ),
                        )
                        conn.commit()
                        report.stops_repaired.append(c_order_id)

                    elif b_stop < curr_stop - tick_size:
                        # Tighter -> Adopt locally
                        conn.execute("BEGIN IMMEDIATE")
                        conn.execute(
                            """
                            UPDATE orders_and_positions
                            SET current_stop_price = ?, stop_version = stop_version + 1, updated_at = ?
                            WHERE client_order_id = ?
                            """,
                            (b_stop, current_time_ms, c_order_id),
                        )
                        conn.commit()
                        report.stops_adopted.append(c_order_id)

        elif status in (OrderStatus.SUBMITTING, OrderStatus.SUBMITTED):
            # Late-Fill Orphan Adoption Check
            if b_positions:
                b_pos = b_positions[0]
                b_dir_raw = _get_attr(b_pos, "direction")
                b_dir = TradeDirection.BUY if b_dir_raw == "BUY" else TradeDirection.SELL_SHORT
                b_size = float(_get_attr(b_pos, "size", 0.0))
                b_fill = float(_get_attr(b_pos, "level", fill_price or curr_stop))
                deal_id = _get_attr(b_pos, "deal_id")

                if b_dir == direction and abs(b_size - units) < 1e-4:
                    # Composite Match: Adopt position!
                    matched_broker_epics.add(epic)
                    conn.execute("BEGIN IMMEDIATE")
                    conn.execute(
                        """
                        UPDATE orders_and_positions
                        SET order_status = 'OPEN', deal_id = ?, actual_fill_price = ?,
                            opened_at = ?, trailing_high_watermark = ?, trailing_low_watermark = ?,
                            updated_at = ?
                        WHERE client_order_id = ?
                        """,
                        (deal_id, b_fill, current_time_ms, b_fill, b_fill, current_time_ms, c_order_id),
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
                            json.dumps({"action": "Late-fill orphan adopted to OPEN", "deal_id": deal_id}),
                            c_order_id,
                        ),
                    )
                    conn.commit()
                    report.orphans_adopted.append(c_order_id)

    # -------------------------------------------------------------------------
    # PASS 2: Detect Foreign Unowned Positions on Broker
    # -------------------------------------------------------------------------
    for epic, b_positions in broker_by_epic.items():
        if epic not in matched_broker_epics:
            # Foreign unowned position!
            block_row = conn.execute(
                "SELECT block_id FROM epic_blocks WHERE epic = ? AND unblocked_at IS NULL",
                (epic,),
            ).fetchone()
            if not block_row:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "INSERT INTO epic_blocks (epic, reason, blocked_at) VALUES (?, ?, ?)",
                    (epic, BlockReason.MANUAL_POSITION_EXISTS.value, current_time_ms),
                )
                conn.execute(
                    """
                    INSERT INTO system_audit_log (
                        timestamp, event_type, discrepancy_detected, details_json
                    ) VALUES (?, ?, 1, ?)
                    """,
                    (
                        current_time_ms,
                        AuditEventType.EPIC_BLOCKED.value,
                        json.dumps({"epic": epic, "reason": "Foreign unowned position on broker"}),
                    ),
                )
                conn.commit()
                report.blocked_foreign_epics.append(epic)

    return report
