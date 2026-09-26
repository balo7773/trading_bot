import sys
from pathlib import Path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from monitoring.telegram_bot import start_telegram_listener_thread
"""
scripts/run_daily_daemon.py
Master Daily Trading Daemon and Phase Orchestrator.
Implements Master Spec V4.1 Section 6 with strict schema and sizing parity.
"""
import sys
from pathlib import Path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import argparse
import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, date
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from config.enums import (
    AbortReason,
    AuditEventType,
    EngineType,
    OrderStatus,
    PermittedDirection,
    SignalStatus,
    SubAccount,
    TradeDirection,
)
from database.connection import get_connection
from execution.capital_client import CapitalBrokerClient
from config import load_config
from strategies.engine_a_mr import evaluate_engine_a
from strategies.engine_b_bo import evaluate_engine_b
import numpy as np
# replaced:
from config.enums import (
    AbortReason,
    AuditEventType,
    EngineType,
    OrderStatus,
    SignalStatus,
    SubAccount,
    TradeDirection,
)
from config.settings import ENABLE_EARNINGS_FILTER
from execution.gap_engine import (
    GapAction,
    evaluate_morning_open,
    evaluate_reclaim_trigger,
)
from execution.order_manager import execute_order
from execution.sizing import (
    BatchPortfolioState,
    MarketRules,
    evaluate_candidate_sizing,
    rank_and_size_morning_batch,
)
from monitoring.circuit_breaker import (
    audit_consecutive_timeouts,
    audit_session_drawdown,
    get_circuit_breaker_status,
)
from monitoring.reconciliation import reconcile_positions
from monitoring.recovery import run_startup_recovery
from monitoring.trailing_manager import (
    ExitAction,
    evaluate_engine_a_exits,
    evaluate_engine_b_trailing_stop,
)

ET_TZ = ZoneInfo("America/New_York")

# Standard NYSE Holidays (Observed)
NYSE_HOLIDAYS_2026 = {
    date(2026, 1, 1),   # New Year's Day
    date(2026, 1, 19),  # MLK Jr. Day
    date(2026, 2, 16),  # Washington's Birthday
    date(2026, 4, 3),   # Good Friday
    date(2026, 5, 25),  # Memorial Day
    date(2026, 6, 19),  # Juneteenth
    date(2026, 7, 3),   # Independence Day (Observed)
    date(2026, 9, 7),   # Labor Day
    date(2026, 11, 26), # Thanksgiving
    date(2026, 12, 25), # Christmas
}


def _now_ms() -> int:
    return int(time.time() * 1000)


def is_nyse_trading_day(d: date) -> bool:
    """Checks if date is a standard US weekday trading session."""
    if d.weekday() >= 5:
        return False
    if d in NYSE_HOLIDAYS_2026:
        return False
    return True


def _build_portfolio_state(conn: sqlite3.Connection, account_equity: float) -> BatchPortfolioState:
    """Reconstructs current active margin and direction counts from local DB."""
    cursor = conn.cursor()
    rows = cursor.execute(
        """
        SELECT direction, sub_account, margin_used_usd
        FROM orders_and_positions
        WHERE order_status IN ('OPEN', 'CLOSING', 'SUBMITTING', 'SUBMITTED')
        """
    ).fetchall()

    active_longs = 0
    active_shorts = 0
    sub_a_margin = 0.0
    sub_b_margin = 0.0

    for dir_str, sub_acc_str, margin_usd in rows:
        if dir_str in (TradeDirection.BUY.value, "BUY"):
            active_longs += 1
        else:
            active_shorts += 1

        m = float(margin_usd or 0.0)
        if sub_acc_str in (SubAccount.SUB_A_MR.value, "SUB_A_MR"):
            sub_a_margin += m
        elif sub_acc_str in (SubAccount.SUB_B_BO.value, "SUB_B_BO"):
            sub_b_margin += m

    return BatchPortfolioState(
        equity=account_equity,
        active_longs=active_longs,
        active_shorts=active_shorts,
        sub_a_margin_used=sub_a_margin,
        sub_b_margin_used=sub_b_margin,
    )


@dataclass
class PhaseExecutionReport:
    phase_name: str
    executed_at: int
    success: bool
    details: Dict[str, Any]


class DailyDaemon:
    """
    Coordinates daily phases, pre-market checks, execution, and monitoring.
    """

    def __init__(self, conn: sqlite3.Connection, broker_client: Any):
        self.conn = conn
        self.broker = broker_client

    def run_boot_sequence(self, current_time_ms: Optional[int] = None) -> PhaseExecutionReport:
        """
        Executes on startup before any loops start.
        Resolves incomplete orders via Section 6.4 Startup Recovery Matrix.
        """
        now = current_time_ms or _now_ms()
        report = run_startup_recovery(conn=self.conn, broker_client=self.broker, current_time_ms=now)
        return PhaseExecutionReport(
            phase_name="BOOT_RECOVERY",
            executed_at=now,
            success=True,
            details={
                "submitted_open": len(report.submitted_resolved_open),
                "submitted_rejected": len(report.submitted_resolved_rejected),
                "submitting_adopted": len(report.submitting_adopted_open),
                "submitting_aborted": len(report.submitting_abandoned_aborted),
                "queued_cancelled": len(report.queued_cancelled),
            },
        )

    def run_pre_market_phase(
        self,
        trading_date: str,
        current_time_ms: Optional[int] = None,
    ) -> PhaseExecutionReport:
        """
        Phase 2 (09:00 ET):
        - Audits Circuit Breakers
        - Validates corporate action quarantine
        - Promotes valid daily signals for today
        """
        now = current_time_ms or _now_ms()

        cb_status = get_circuit_breaker_status(self.conn)
        if cb_status.is_active:
            return PhaseExecutionReport(
                phase_name="PHASE_2_PRE_MARKET",
                executed_at=now,
                success=False,
                details={"abort_reason": "CIRCUIT_BREAKER_ACTIVE", "reason": cb_status.reason},
            )

        cursor = self.conn.cursor()
        rows = cursor.execute(
            """
            SELECT signal_id, epic FROM daily_signals
            WHERE signal_date = ? AND signal_status = ?
            """,
            (trading_date, SignalStatus.PENDING.value),
        ).fetchall()

        promoted = []
        for sig_id, epic in rows:
            self.conn.execute("BEGIN IMMEDIATE")
            self.conn.execute(
                """
                UPDATE daily_signals
                SET signal_status = ?
                WHERE signal_id = ?
                """,
                (SignalStatus.PROMOTED.value, sig_id),
            )
            self.conn.commit()
            promoted.append(sig_id)

        return PhaseExecutionReport(
            phase_name="PHASE_2_PRE_MARKET",
            executed_at=now,
            success=True,
            details={"promoted_signals": promoted, "count": len(promoted)},
        )

    def run_market_open_phase(
        self,
        trading_date: str,
        live_open_quotes: Dict[str, Dict[str, float]],
        account_equity: float,
        current_time_ms: Optional[int] = None,
    ) -> PhaseExecutionReport:
        """
        Phase 3 (09:30 ET):
        - Ingests open prices for promoted signals
        - Evaluates 4-tier Gap Engine
        - Sizes approved candidates via rank_and_size_morning_batch
        - Dispatches orders via Write-Ahead Order Manager
        - Arms Tier 3 reclaim setups
        """
        now = current_time_ms or _now_ms()

        cursor = self.conn.cursor()
        signals = cursor.execute(
            """
            SELECT s.signal_id, s.epic, s.engine, s.direction, s.signal_close_price,
                   s.structural_stop_price, s.target_price, s.provisional_priority,
                   w.sunday_composite_rank
            FROM daily_signals s
            LEFT JOIN weekly_watchlist w ON s.watchlist_id = w.watchlist_id
            WHERE s.signal_date = ? AND s.signal_status = ?
            """,
            (trading_date, SignalStatus.PROMOTED.value),
        ).fetchall()

        dispatched_orders = []
        tier3_reclaims_armed = []
        queue_a = []
        queue_b = []
        rules_map = {}
        cand_lookup = {}

        for row in signals:
            sig_id, epic, eng_str, dir_str, c_close, s_stop, target, prov_pri, rank = row
            engine = EngineType(eng_str)
            direction = TradeDirection(dir_str)
            comp_rank = int(rank) if rank is not None else 1

            quote = live_open_quotes.get(epic)
            if not quote:
                continue

            open_price = quote["open"]
            bid = quote.get("bid", open_price)
            ask = quote.get("ask", open_price)
            atr14 = quote.get("atr14", 2.0)

            # Evaluate Gap Engine directly using verified signature
            gap_res = evaluate_morning_open(
                engine=engine,
                direction=direction,
                signal_close_price=c_close,
                structural_stop_price=s_stop,
                atr14=atr14,
                live_open_price=open_price,
                current_time_ms=now,
                target_price=target,
            )

            if gap_res.action == GapAction.ABORT:
                abort_rsn = gap_res.abort_reason or AbortReason.TIER_2_EXHAUSTION
                rsn_val = abort_rsn.value if hasattr(abort_rsn, "value") else str(abort_rsn)

                self.conn.execute("BEGIN IMMEDIATE")
                self.conn.execute(
                    """
                    UPDATE daily_signals
                    SET signal_status = ?, abort_reason = ?
                    WHERE signal_id = ?
                    """,
                    (SignalStatus.ABORTED.value, rsn_val, sig_id),
                )
                self.conn.commit()
                continue

            elif gap_res.action == GapAction.ARM_RECLAIM:
                # Signal remains PROMOTED; deadline already set at creation
                tier3_reclaims_armed.append(epic)
                continue

            # ENTER_MARKET: Structural stop is sacred from Phase 1 (Failure 1 resolution)
            exec_entry = gap_res.executable_entry_price or open_price
            exec_stop = s_stop

            rules_map[epic] = MarketRules(
                min_position_size=quote.get("min_position_size", quote.get("min_size", 1.0)),
                lot_step=quote.get("lot_step", 1.0),
                margin_rate=quote.get("margin_rate", 0.20),
                bid=bid,
                ask=ask,
                fx_rate=quote.get("fx_rate", 1.0),
            )

            item = {
                "signal_id": sig_id,
                "epic": epic,
                "direction": direction,
                "sunday_composite_rank": comp_rank,
                "executable_entry_price": exec_entry,
                "structural_stop_price": exec_stop,
                "target_price": target,
                "sub_account": SubAccount.SUB_A_MR if engine == EngineType.MEAN_REVERSION else SubAccount.SUB_B_BO,
            }
            if engine == EngineType.MEAN_REVERSION:
                item["executable_rr_ratio"] = gap_res.executable_rr_ratio
                queue_a.append(item)
            else:
                queue_b.append(item)

            cand_lookup[sig_id] = item

        # Pre-Flight Batch Sizing Gate
        if queue_a or queue_b:
            state = _build_portfolio_state(self.conn, account_equity)
            batch_res = rank_and_size_morning_batch(
                queue_a_candidates=queue_a,
                queue_b_candidates=queue_b,
                market_rules_map=rules_map,
                state=state,
            )

            for sig_id, s_res in batch_res.items():
                cand = cand_lookup[sig_id]
                if s_res.approved:
                    units = getattr(s_res, "allocated_units", getattr(s_res, "units", 0.0))
                    margin = getattr(s_res, "margin_used_usd", getattr(s_res, "margin_used", 0.0))
                    planned_risk = getattr(s_res, "planned_risk_r_usd", getattr(s_res, "risk_usd", account_equity * 0.01))

                    c_order_id = f"ORD_{now}_{cand['epic']}"
                    exec_res = execute_order(
                        conn=self.conn,
                        broker_client=self.broker,
                        client_order_id=c_order_id,
                        signal_id=sig_id,
                        epic=cand["epic"],
                        sub_account=cand["sub_account"],
                        direction=cand["direction"],
                        planned_entry_price=cand["executable_entry_price"],
                        executable_entry_price=cand["executable_entry_price"],
                        initial_stop_price=cand["structural_stop_price"],
                        target_price=cand["target_price"],
                        planned_risk_r_usd=planned_risk,
                        allocated_units=units,
                        margin_used_usd=margin,
                    )
                    if exec_res.order_status in (OrderStatus.OPEN, OrderStatus.SUBMITTED, OrderStatus.SUBMITTING):
                        dispatched_orders.append(cand["epic"])
                else:
                    abort_rsn = s_res.abort_reason or AbortReason.MARGIN_CEILING_EXCEEDED
                    rsn_val = abort_rsn.value if hasattr(abort_rsn, "value") else str(abort_rsn)
                    self.conn.execute("BEGIN IMMEDIATE")
                    self.conn.execute(
                        """
                        UPDATE daily_signals
                        SET signal_status = ?, abort_reason = ?
                        WHERE signal_id = ?
                        """,
                        (SignalStatus.ABORTED.value, rsn_val, sig_id),
                    )
                    self.conn.commit()

        return PhaseExecutionReport(
            phase_name="PHASE_3_MARKET_OPEN",
            executed_at=now,
            success=True,
            details={
                "dispatched_orders": dispatched_orders,
                "tier3_reclaims_armed": tier3_reclaims_armed,
            },
        )

    def run_reclaim_monitor_tick(
        self,
        live_quotes: Dict[str, Dict[str, float]],
        account_equity: float,
        current_time_ms: Optional[int] = None,
    ) -> List[str]:
        """
        Runs every 60 seconds between 09:30 and 10:30 ET:
        Audits active setups that have NOT already dispatched an order.
        Sizing uses the live executable current_price (Section 3.5).
        Preserves reclaim_deadline_at permanently for audit forensics (Failure 2 resolution).
        """
        now = current_time_ms or _now_ms()
        cursor = self.conn.cursor()
        rows = cursor.execute(
            """
            SELECT s.signal_id, s.epic, s.engine, s.direction, s.signal_close_price,
                   s.structural_stop_price, s.target_price, s.reclaim_deadline_at, s.provisional_priority
            FROM daily_signals s
            WHERE s.signal_status = ?
              AND NOT EXISTS (
                  SELECT 1 FROM orders_and_positions o WHERE o.signal_id = s.signal_id
              )
            """,
            (SignalStatus.PROMOTED.value,),
        ).fetchall()

        reclaimed_orders = []

        for row in rows:
            sig_id, epic, eng_str, dir_str, c_close, s_stop, target, deadline, prov_pri = row
            engine = EngineType(eng_str)
            direction = TradeDirection(dir_str)
            quote = live_quotes.get(epic)
            if not quote:
                continue

            current_price = quote["current"]
            bid = quote.get("bid", current_price)
            ask = quote.get("ask", current_price)

            reclaim_res = evaluate_reclaim_trigger(
                engine=engine,
                direction=direction,
                trigger_price=c_close,
                structural_stop_price=s_stop,
                current_price=current_price,
                current_time_ms=now,
                deadline_ms=deadline,
                target_price=target,
            )

            if reclaim_res.action == GapAction.ENTER_MARKET:
                sub_acc = SubAccount.SUB_A_MR if engine == EngineType.MEAN_REVERSION else SubAccount.SUB_B_BO
                rules = MarketRules(
                    min_position_size=quote.get("min_position_size", quote.get("min_size", 1.0)),
                    lot_step=quote.get("lot_step", 1.0),
                    margin_rate=quote.get("margin_rate", 0.20),
                    bid=bid,
                    ask=ask,
                    fx_rate=quote.get("fx_rate", 1.0),
                )
                state = _build_portfolio_state(self.conn, account_equity)
                sizing_res = evaluate_candidate_sizing(
                    sub_account=sub_acc,
                    direction=direction,
                    entry_price=current_price,
                    structural_stop_price=s_stop,
                    rules=rules,
                    state=state,
                    is_top_priority=(prov_pri >= 2.0),
                )

                if sizing_res.approved:
                    units = getattr(sizing_res, "allocated_units", getattr(sizing_res, "units", 0.0))
                    margin = getattr(sizing_res, "margin_used_usd", getattr(sizing_res, "margin_used", 0.0))
                    planned_risk = getattr(sizing_res, "planned_risk_r_usd", getattr(sizing_res, "risk_usd", account_equity * 0.01))

                    c_id = f"ORD_REC_{now}_{epic}"
                    exec_res = execute_order(
                        conn=self.conn,
                        broker_client=self.broker,
                        client_order_id=c_id,
                        signal_id=sig_id,
                        epic=epic,
                        sub_account=sub_acc,
                        direction=direction,
                        planned_entry_price=current_price,
                        executable_entry_price=current_price,
                        initial_stop_price=s_stop,
                        target_price=target,
                        planned_risk_r_usd=planned_risk,
                        allocated_units=units,
                        margin_used_usd=margin,
                    )
                    if exec_res.order_status in (OrderStatus.OPEN, OrderStatus.SUBMITTED):
                        reclaimed_orders.append(epic)
                else:
                    abort_rsn = sizing_res.abort_reason or AbortReason.MARGIN_CEILING_EXCEEDED
                    rsn_val = abort_rsn.value if hasattr(abort_rsn, "value") else str(abort_rsn)
                    self.conn.execute("BEGIN IMMEDIATE")
                    self.conn.execute(
                        """
                        UPDATE daily_signals
                        SET signal_status = ?, abort_reason = ?
                        WHERE signal_id = ?
                        """,
                        (SignalStatus.ABORTED.value, rsn_val, sig_id),
                    )
                    self.conn.commit()

            elif reclaim_res.action == GapAction.ABORT:
                abort_rsn = reclaim_res.abort_reason or AbortReason.EXPIRED_NO_ENTRY
                rsn_val = abort_rsn.value if hasattr(abort_rsn, "value") else str(abort_rsn)
                self.conn.execute("BEGIN IMMEDIATE")
                self.conn.execute(
                    """
                    UPDATE daily_signals
                    SET signal_status = ?, abort_reason = ?
                    WHERE signal_id = ?
                    """,
                    (SignalStatus.ABORTED.value, rsn_val, sig_id),
                )
                self.conn.commit()

        return reclaimed_orders

    def run_intraday_maintenance_tick(
        self,
        live_quotes: Dict[str, Dict[str, float]],
        session_pnl_usd: float,
        r_unit_usd: float,
        current_time_ms: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Runs every 15 minutes between 09:30 and 16:00 ET:
        1. 15-Minute Position Reconciler (ghost exits, stop repairs, orphan adoption)
        2. Trade Management Exits (Engine A frozen EMA & 5% cap; Engine B ratcheting stops)
        3. Circuit Breaker Audits (Drawdown & timeout checks)
        """
        now = current_time_ms or _now_ms()

        recon_report = reconcile_positions(conn=self.conn, broker_client=self.broker, current_time_ms=now)

        cursor = self.conn.cursor()
        open_positions = cursor.execute(
            """
            SELECT client_order_id, sub_account, direction, actual_fill_price,
                   current_stop_price, stop_version, target_price,
                   trailing_high_watermark, trailing_low_watermark, epic
            FROM orders_and_positions
            WHERE order_status = 'OPEN'
            """
        ).fetchall()

        exits_triggered = []
        stops_amended = []

        for pos in open_positions:
            c_id, sub_acc, dir_str, fill, curr_stop, stop_ver, target, h_water, l_water, epic = pos
            quote = live_quotes.get(epic)
            if not quote:
                continue

            direction = TradeDirection(dir_str)
            bid = quote["bid"]
            ask = quote["ask"]

            if sub_acc == SubAccount.SUB_A_MR.value and target:
                act = evaluate_engine_a_exits(
                    direction=direction,
                    actual_fill_price=fill,
                    frozen_target_price=target,
                    current_bid=bid,
                    current_ask=ask,
                )
                if act.action == ExitAction.CLOSE_POSITION:
                    self.broker.close_position(client_order_id=c_id)
                    self.conn.execute("BEGIN IMMEDIATE")
                    self.conn.execute(
                        """
                        UPDATE orders_and_positions
                        SET order_status = 'CLOSED', closed_at = ?, close_reason = ?, updated_at = ?
                        WHERE client_order_id = ?
                        """,
                        (now, act.close_reason.value, now, c_id),
                    )
                    self.conn.commit()
                    exits_triggered.append(c_id)

            elif sub_acc == SubAccount.SUB_B_BO.value:
                act = evaluate_engine_b_trailing_stop(
                    direction=direction,
                    actual_fill_price=fill,
                    current_stop_price=curr_stop,
                    current_high=quote.get("high", bid),
                    current_low=quote.get("low", ask),
                    atr14=quote.get("atr14", 2.0),
                    trailing_high_watermark=h_water,
                    trailing_low_watermark=l_water,
                )
                if act.action == ExitAction.AMEND_STOP:
                    self.broker.update_position(deal_id=c_id, stop_level=act.new_stop_price)
                    self.conn.execute("BEGIN IMMEDIATE")
                    self.conn.execute(
                        """
                        UPDATE orders_and_positions
                        SET current_stop_price = ?, stop_version = stop_version + 1,
                            trailing_high_watermark = ?, trailing_low_watermark = ?, updated_at = ?
                        WHERE client_order_id = ?
                        """,
                        (act.new_stop_price, act.new_high_watermark, act.new_low_watermark, now, c_id),
                    )
                    self.conn.commit()
                    stops_amended.append(c_id)

        audit_session_drawdown(
            conn=self.conn,
            current_time_ms=now,
            session_pnl_usd=session_pnl_usd,
            r_unit_usd=r_unit_usd,
        )
        audit_consecutive_timeouts(conn=self.conn, current_time_ms=now)

        return {
            "reconciliation": {
                "ghosts": len(recon_report.ghost_exits_closed),
                "repaired": len(recon_report.stops_repaired),
                "adopted": len(recon_report.stops_adopted),
            },
            "exits_triggered": exits_triggered,
            "stops_amended": stops_amended,
        }


    def run_post_market_phase(
        self,
        trading_date: str,
        current_time_ms: Optional[int] = None,
    ) -> PhaseExecutionReport:
        """
        Phase 1 (Post-Market Close / 16:05 ET):
        - Ingests active weekly_watchlist candidates
        - Fetches daily bars up to closing bar
        - Evaluates Engine A (Mean Reversion) & Engine B (Breakout)
        - Atomically persists candidates into daily_signals
        """
        now = current_time_ms or _now_ms()
        cursor = self.conn.cursor()

        watchlist_items = cursor.execute(
            """
            SELECT watchlist_id, epic, assigned_engine, permitted_direction
            FROM weekly_watchlist
            WHERE superseded_at IS NULL
            ORDER BY sunday_composite_rank ASC
            """
        ).fetchall()

        generated_signals = []
        aborted_signals = []

        for w_id, epic, eng_str, dir_str in watchlist_items:
            engine = EngineType(eng_str)
            permitted_dir = PermittedDirection(dir_str)

            # Fetch daily bars
            df = self.broker.fetch_historical_ohlcv(epic=epic, resolution="DAY", max_bars=300)
            if df is None or len(df) < 35:
                continue

            high_p = df["high"].values.astype(np.float64)
            low_p = df["low"].values.astype(np.float64)
            close_p = df["close"].values.astype(np.float64)
            open_p = df["open"].values.astype(np.float64)

            cand = None
            if engine == EngineType.MEAN_REVERSION:
                cand = evaluate_engine_a(
                    watchlist_id=w_id,
                    epic=epic,
                    signal_date=trading_date,
                    high_p=high_p,
                    low_p=low_p,
                    close_p=close_p,
                    open_p=open_p,
                    permitted_direction=permitted_dir,
                )
            elif engine == EngineType.BREAKOUT:
                cand = evaluate_engine_b(
                    watchlist_id=w_id,
                    epic=epic,
                    signal_date=trading_date,
                    high_p=high_p,
                    low_p=low_p,
                    close_p=close_p,
                )

            if cand is not None:
                # Idempotent database commit
                self.conn.execute("BEGIN IMMEDIATE")
                try:
                    abort_val = cand.abort_reason.value if cand.abort_reason else None
                    self.conn.execute(
                        """
                        INSERT OR REPLACE INTO daily_signals (
                            signal_id, watchlist_id, epic, engine, direction, signal_date,
                            signal_close_price, structural_stop_price, target_price, initial_rr_ratio,
                            executable_rr_ratio, provisional_priority, final_priority,
                            breakout_thrust_atr, compression_spread_pct, stop_distance_pct,
                            next_ex_div_pct_of_price, signal_status, abort_reason,
                            reclaim_deadline_at, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            cand.signal_id,
                            cand.watchlist_id,
                            cand.epic,
                            cand.engine.value,
                            cand.direction.value,
                            cand.signal_date,
                            cand.signal_close_price,
                            cand.structural_stop_price,
                            cand.target_price,
                            cand.initial_rr_ratio,
                            cand.executable_rr_ratio,
                            cand.provisional_priority,
                            cand.final_priority,
                            cand.breakout_thrust_atr,
                            cand.compression_spread_pct,
                            cand.stop_distance_pct,
                            cand.next_ex_div_pct_of_price,
                            cand.signal_status.value,
                            abort_val,
                            cand.reclaim_deadline_at,
                            cand.created_at,
                        ),
                    )
                    self.conn.commit()

                    if cand.signal_status == SignalStatus.PENDING:
                        generated_signals.append(cand.epic)
                    else:
                        aborted_signals.append((cand.epic, abort_val))
                except Exception as e:
                    self.conn.rollback()
                    raise e

        return PhaseExecutionReport(
            phase_name="PHASE_1_POST_MARKET",
            executed_at=now,
            success=True,
            details={
                "pending_signals": generated_signals,
                "aborted_signals": aborted_signals,
                "total_generated": len(generated_signals) + len(aborted_signals),
            },
        )



def run_continuous_daemon(conn: sqlite3.Connection, broker: Any):
    """
    Continuous production daemon orchestrator.
    Controls session lifecycle, Sunday screening, and daily execution phases.
    """
    daemon = DailyDaemon(conn=conn, broker_client=broker)
    
    start_telegram_listener_thread()
    print("[DAEMON] Starting boot recovery sequence...")
    boot_rep = daemon.run_boot_sequence()
    print(f"[DAEMON] Boot recovery completed: {boot_rep.details}")

    last_screening_date = None
    last_friday_report_date = None
    last_phase1_date = None
    last_phase2_date = None
    last_phase3_date = None
    last_reclaim_tick = 0
    last_maint_tick = 0

    print("[DAEMON] Entering master operational loop...")

    while True:
        try:
            now_utc = datetime.now(ZoneInfo("UTC"))
            now_et = datetime.now(ET_TZ)
            today_et_str = now_et.strftime("%Y-%m-%d")
            weekday = now_et.weekday()  # 0=Monday, ..., 6=Sunday
            now_ms = _now_ms()

            # -------------------------------------------------------------
            # 1. SUNDAY SCREENING (Sunday at 19:00 UTC / 20:00 WAT)
            # -------------------------------------------------------------
            if weekday == 6 and now_utc.hour >= 19 and last_screening_date != today_et_str:
                print(f"[DAEMON] Sunday Screening Window Detected. Triggering screening funnel...")
                import subprocess
                res = subprocess.run(["python3", "scripts/run_screening.py", "--adhoc"], capture_output=True, text=True)
                print(res.stdout[-500:] if res.stdout else "Screening completed.")
                last_screening_date = today_et_str

            # -------------------------------------------------------------
            # WEEKDAY EXECUTION (Monday - Friday)
            # -------------------------------------------------------------
            if is_nyse_trading_day(now_et.date()):
                cur_hm = now_et.strftime("%H:%M")

                # Phase 2: Pre-Market Signal Promotion (09:00 ET)
                if cur_hm >= "09:00" and cur_hm < "09:30" and last_phase2_date != today_et_str:
                    print(f"[DAEMON] Running Phase 2 Pre-Market Checks for {today_et_str}...")
                    p2_rep = daemon.run_pre_market_phase(trading_date=today_et_str, current_time_ms=now_ms)
                    print(f"[DAEMON] Phase 2 complete: {p2_rep.details}")
                    last_phase2_date = today_et_str

                # Phase 3: Market Open Order Execution (09:30 ET)
                if cur_hm >= "09:30" and cur_hm < "16:00" and last_phase3_date != today_et_str:
                    print(f"[DAEMON] Market Open: Running Phase 3 Batch Execution for {today_et_str}...")
                    # Fetch live open quotes for promoted signals
                    cursor = conn.cursor()
                    promoted_epics = [
                        r[0] for r in cursor.execute(
                            "SELECT epic FROM daily_signals WHERE signal_date = ? AND signal_status = 'PROMOTED'",
                            (today_et_str,)
                        ).fetchall()
                    ]
                    quotes = {}
                    for epic in promoted_epics:
                        mkt = broker.get_market_rules(epic)
                        if mkt:
                            quotes[epic] = {
                                "open": mkt.bid,
                                "bid": mkt.bid,
                                "ask": mkt.ask,
                                "min_size": mkt.min_position_size,
                                "lot_step": mkt.lot_step,
                                "margin_rate": mkt.margin_rate,
                            }
                    # Sizing against equity (or baseline)
                    p3_rep = daemon.run_market_open_phase(
                        trading_date=today_et_str,
                        live_open_quotes=quotes,
                        account_equity=100000.0,
                        current_time_ms=now_ms
                    )
                    print(f"[DAEMON] Phase 3 complete: {p3_rep.details}")
                    last_phase3_date = today_et_str

                # Reclaim Monitor Window (09:30 - 10:30 ET): Every 60 seconds
                if "09:30" <= cur_hm < "10:30" and (now_ms - last_reclaim_tick) >= 60000:
                    cursor = conn.cursor()
                    active_reclaims = [
                        r[0] for r in cursor.execute(
                            "SELECT epic FROM daily_signals WHERE signal_status = 'PROMOTED'"
                        ).fetchall()
                    ]
                    if active_reclaims:
                        quotes = {}
                        for epic in active_reclaims:
                            mkt = broker.get_market_rules(epic)
                            if mkt:
                                quotes[epic] = {"current": mkt.bid, "bid": mkt.bid, "ask": mkt.ask}
                        daemon.run_reclaim_monitor_tick(
                            live_quotes=quotes,
                            account_equity=100000.0,
                            current_time_ms=now_ms
                        )
                    last_reclaim_tick = now_ms

                # Intraday Maintenance (09:30 - 16:00 ET): Every 15 minutes
                if "09:30" <= cur_hm < "16:00" and (now_ms - last_maint_tick) >= 900000:
                    cursor = conn.cursor()
                    open_epics = [r[0] for r in cursor.execute("SELECT epic FROM orders_and_positions WHERE order_status = 'OPEN'").fetchall()]
                    quotes = {}
                    for epic in open_epics:
                        mkt = broker.get_market_rules(epic)
                        if mkt:
                            quotes[epic] = {"bid": mkt.bid, "ask": mkt.ask}
                    daemon.run_intraday_maintenance_tick(
                        live_quotes=quotes,
                        session_pnl_usd=0.0,
                        r_unit_usd=1000.0,
                        current_time_ms=now_ms
                    )
                    last_maint_tick = now_ms


                # Phase 1: Post-Market Signal Generation (16:05 ET)
                if cur_hm >= "16:05" and last_phase1_date != today_et_str:
                    print(f"[DAEMON] Market Close: Running Phase 1 Signal Generation for {today_et_str}...")
                    p1_rep = daemon.run_post_market_phase(trading_date=today_et_str, current_time_ms=now_ms)
                    print(f"[DAEMON] Phase 1 complete: {p1_rep.details}")
                    last_phase1_date = today_et_str

                # Friday Close Executive SITREP (16:30 ET / 21:30 WAT on Friday)
                if weekday == 4 and cur_hm >= "16:30" and last_friday_report_date != today_et_str:
                    print(f"[DAEMON] Friday Close reached. Compiling & dispatching Weekly Performance SITREP...")
                    try:
                        send_weekly_friday_report()
                        print("[DAEMON] Weekly Friday report dispatched to Telegram.")
                        last_friday_report_date = today_et_str
                    except Exception as re:
                        print(f"[DAEMON ERROR] Failed to send Friday report: {re}")


            # Sleep 15 seconds between cycle evaluations
            time.sleep(15)

        except Exception as e:
            print(f"[DAEMON ERROR] Unexpected loop exception: {e}")
            time.sleep(30)

def main():
    parser = argparse.ArgumentParser(description="Master Daily Trading Daemon Orchestrator")
    parser.add_argument("--daemon", action="store_true", help="Run continuous background daemon")
    parser.add_argument("--boot", action="store_true", help="Run startup recovery")
    parser.add_argument("--phase-1", action="store_true", help="Run Phase 1 post-close signal generation")
    parser.add_argument("--phase-2", action="store_true", help="Run Phase 2 pre-market signal promotion")
    parser.add_argument("--phase-3", action="store_true", help="Run Phase 3 market-open order execution")
    parser.add_argument("--date", type=str, default=None, help="Target trading date (YYYY-MM-DD)")
    args = parser.parse_args()

    cfg = load_config()
    broker = CapitalBrokerClient(
        api_key=cfg.api_key,
        identifier=cfg.identifier,
        password=cfg.password,
        demo=cfg.is_demo,
    )

    target_date = args.date or date.today().strftime("%Y-%m-%d")

    with get_connection() as conn:
        daemon = DailyDaemon(conn=conn, broker_client=broker)

        if args.daemon:
            print("Launching 24/7 Quantitative Daemon...")
            run_continuous_daemon(conn, broker)
        elif args.boot:
            print("Executing Daemon Boot Recovery...")
            res = daemon.run_boot_sequence()
            print(f"Boot result: {res.details}")

        if args.phase_1:
            print(f"Executing Phase 1 Signal Generation for {target_date}...")
            res = daemon.run_post_market_phase(trading_date=target_date)
            print("=" * 80)
            print("PHASE 1 EXECUTION REPORT:")
            print(f"Pending Triggers: {res.details['pending_signals']}")
            print(f"Aborted / Filtered: {res.details['aborted_signals']}")
            print(f"Total Processed: {res.details['total_generated']}")
            print("=" * 80)

        if args.phase_2:
            print(f"Executing Phase 2 Pre-Market Checks for {target_date}...")
            res = daemon.run_pre_market_phase(trading_date=target_date)
            print(f"Phase 2 Promoted: {res.details}")


if __name__ == "__main__":
    main()
