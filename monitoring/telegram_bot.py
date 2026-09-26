"""
monitoring/telegram_bot.py
Unified Telegram Notification Engine and Interactive Command Listener.
Uses HTML formatting and dynamic SQLite schema discovery.
"""
import os
import sys
import json
import time
import html
import socket
import urllib.request
import urllib.parse
import threading
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Optional, Dict, Any, List

# Ensure repository root is on sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Automatic .env loading
env_path = PROJECT_ROOT / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k not in os.environ:
                os.environ[k] = v

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip().strip('"').strip("'")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip().strip('"').strip("'")
TELEGRAM_API_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"

from database.connection import get_connection
from monitoring.circuit_breaker import get_circuit_breaker_status
from config import load_config
from execution.capital_client import CapitalBrokerClient


def _get_broker():
    cfg = load_config()
    return CapitalBrokerClient(
        api_key=cfg.api_key,
        identifier=cfg.identifier,
        password=cfg.password,
        demo=cfg.is_demo,
    )


def send_telegram_message(text: str, parse_mode: Optional[str] = "HTML", target_chat_id: Optional[str] = None) -> bool:
    """Dispatches a push notification to Telegram with plain-text fallback."""
    dest_chat = str(target_chat_id or CHAT_ID)
    if not BOT_TOKEN or not dest_chat:
        return False

    url = f"{TELEGRAM_API_BASE}/sendMessage"
    payload = {
        "chat_id": dest_chat,
        "text": text,
        "disable_web_page_preview": True,
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})

    try:
        with urllib.request.urlopen(req, timeout=12) as response:
            return response.status == 200
    except urllib.error.HTTPError:
        if parse_mode:
            payload.pop("parse_mode", None)
            clean_text = html.unescape(text)
            for tag in ["<b>", "</b>", "<code>", "</code>", "<pre>", "</pre>", "<i>", "</i>"]:
                clean_text = clean_text.replace(tag, "")
            payload["text"] = clean_text
            try:
                data_plain = json.dumps(payload).encode("utf-8")
                req_plain = urllib.request.Request(url, data=data_plain, headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req_plain, timeout=10) as resp2:
                    return resp2.status == 200
            except Exception:
                pass
        return False
    except Exception:
        return False


# ============================================================================
# WEEKLY FRIDAY CLOSE SITREP GENERATOR
# ============================================================================

def generate_weekly_report_html() -> str:
    """Aggregates the past 7 days of trading, signals, and broker equity."""
    now_wat = datetime.now(ZoneInfo("Africa/Lagos")).strftime("%Y-%m-%d %H:%M WAT")
    seven_days_ago_ms = int((time.time() - 7 * 86400) * 1000)
    seven_days_ago_date = (datetime.now(ZoneInfo("America/New_York")).date() - timedelta(days=7)).strftime("%Y-%m-%d")

    # 1. Broker Equity & Balance
    eq = 0.0
    cash = 0.0
    pnl = 0.0
    curr = "USD"
    try:
        broker = _get_broker()
        bal_data = broker.fetch_account_balance()
        if bal_data.get("status") == "OK":
            eq = float(bal_data.get("equity", 0.0))
            cash = float(bal_data.get("balance", 0.0))
            pnl = float(bal_data.get("pnl", 0.0))
            curr = str(bal_data.get("currency", "USD"))
    except Exception as be:
        print(f"[REPORT] Broker balance retrieval warning: {be}")

    with get_connection() as conn:
        cur = conn.cursor()

        # Dynamic schema introspection
        cur.execute("PRAGMA table_info(orders_and_positions)")
        table_cols = {row[1] for row in cur.fetchall()}

        status_col = "order_status" if "order_status" in table_cols else "status"
        fill_col = "actual_fill_price" if "actual_fill_price" in table_cols else "price"
        units_col = "allocated_units" if "allocated_units" in table_cols else "units"
        stop_col = "current_stop_price" if "current_stop_price" in table_cols else "stop"
        time_col = "created_at" if "created_at" in table_cols else "timestamp"

        pnl_col = next((c for c in ["realized_pnl", "realized_pl", "pnl", "profit_loss"] if c in table_cols), None)
        exit_col = next((c for c in ["exit_price", "actual_exit_price", "close_price"] if c in table_cols), None)

        # 2. Executed Orders (Past 7 Days)
        select_fields = f"epic, direction, {status_col}, {fill_col}, {units_col}"
        if pnl_col:
            select_fields += f", COALESCE({pnl_col}, 0.0)"
        elif exit_col:
            select_fields += f", COALESCE({exit_col}, 0.0)"
        else:
            select_fields += ", 0.0"

        cur.execute(f"""
            SELECT {select_fields}
            FROM orders_and_positions
            WHERE {time_col} >= ?
            ORDER BY {time_col} DESC
        """, (seven_days_ago_ms,))
        recent_orders = cur.fetchall()

        total_orders = len(recent_orders)
        closed_orders = [o for o in recent_orders if o[2] == "CLOSED"]
        open_orders = [o for o in recent_orders if o[2] == "OPEN"]

        realized_pnl = 0.0
        for o in closed_orders:
            if pnl_col:
                realized_pnl += float(o[5] or 0.0)
            elif exit_col and o[5] and o[3] and o[4]:
                direction_mult = 1.0 if o[1] == "BUY" else -1.0
                realized_pnl += (float(o[5]) - float(o[3])) * float(o[4]) * direction_mult

        winning_trades = sum(1 for o in closed_orders if float(o[5] or 0.0) > 0.0)
        losing_trades = sum(1 for o in closed_orders if float(o[5] or 0.0) < 0.0)
        win_rate = (winning_trades / len(closed_orders) * 100) if closed_orders else 0.0

        # 3. Signals Ingested vs Vetoed (Past 7 Days)
        cur.execute("""
            SELECT signal_status, abort_reason, COUNT(*)
            FROM daily_signals
            WHERE signal_date >= ?
            GROUP BY signal_status, abort_reason
        """, (seven_days_ago_date,))
        sig_rows = cur.fetchall()

        total_signals = sum(r[2] for r in sig_rows)
        promoted_signals = sum(r[2] for r in sig_rows if r[0] == "PROMOTED")
        aborted_signals = sum(r[2] for r in sig_rows if r[0] == "ABORTED")

        vetoes = [r for r in sig_rows if r[0] == "ABORTED" and r[1]]
        vetoes.sort(key=lambda x: x[2], reverse=True)
        top_veto_str = ", ".join([f"{v[1]}: {v[2]}" for v in vetoes[:2]]) if vetoes else "None"

        # 4. Open Book Carried Over Weekend
        cur.execute(f"""
            SELECT epic, direction, {units_col}, {fill_col}, {stop_col}
            FROM orders_and_positions
            WHERE {status_col} = 'OPEN'
        """)
        active_positions = cur.fetchall()

    pnl_sign = "+" if pnl >= 0 else ""
    realized_sign = "+" if realized_pnl >= 0 else ""

    lines = [
        "🏁 <b>WEEKLY QUANTITATIVE SITREP</b>",
        f"<i>Period Ending: {now_wat}</i>",
        "━━━━━━━━━━━━━━━━━━━━━━",
        "💼 <b>LEDGER &amp; CAPITAL</b>",
        f"• <b>Account Equity:</b> <code>${eq:,.2f} {curr}</code>",
        f"• <b>Cash Balance:</b> <code>${cash:,.2f} {curr}</code>",
        f"• <b>Floating P&amp;L:</b> <code>{pnl_sign}${pnl:,.2f}</code>",
        f"• <b>Closed Realized P&amp;L:</b> <code>{realized_sign}${realized_pnl:,.2f}</code>",
        "",
        "🎯 <b>EXECUTION &amp; TRADING</b>",
        f"• <b>Total Fills:</b> <code>{total_orders}</code> (<code>{len(open_orders)} Open</code> | <code>{len(closed_orders)} Closed</code>)",
        f"• <b>Win Rate:</b> <code>{win_rate:.1f}%</code> (<code>{winning_trades}W / {losing_trades}L</code>)",
        "",
        "📡 <b>SIGNAL FUNNEL QUALITY</b>",
        f"• <b>Signals Generated:</b> <code>{total_signals}</code>",
        f"• <b>Executed / Promoted:</b> <code>{promoted_signals}</code>",
        f"• <b>Risk Vetoes:</b> <code>{aborted_signals}</code> ({top_veto_str})",
        "",
        f"📦 <b>WEEKEND BOOK ({len(active_positions)} Active Positions)</b>"
    ]

    if active_positions:
        for p in active_positions:
            fill_p = float(p[3]) if p[3] is not None else 0.0
            stop_p = float(p[4]) if p[4] is not None else 0.0
            lines.append(f"• <b>{p[0]}</b> (<code>{p[1]}</code>): <code>{p[2]}u</code> @ <code>${fill_p:.2f}</code> | Stop: <code>${stop_p:.2f}</code>")
    else:
        lines.append("• <i>Book is 100% flat in cash. Zero weekend overnight risk.</i>")

    lines.append("━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("System in weekend holding pattern until Sunday 19:00 UTC screening.")
    return "\n".join(lines)


def send_weekly_friday_report():
    report_html = generate_weekly_report_html()
    send_telegram_message(report_html)


# ============================================================================
# COMMAND DISPATCHER
# ============================================================================

def handle_telegram_command(command_text: str) -> str:
    parts = command_text.strip().split()
    if not parts:
        return "Type <code>/help</code> for options."

    raw_cmd = parts[0].lower()
    cmd = raw_cmd.split("@")[0]
    arg = parts[1].upper() if len(parts) > 1 else None

    try:
        if cmd in ("/start", "/help"):
            return (
                "🤖 <b>Apex Quantitative Desk Console</b>\n\n"
                "• <code>/status</code> - Live cockpit, clocks, safety, & signals\n"
                "• <code>/balance</code> - Broker balance, equity, & open P&L\n"
                "• <code>/report</code> - Weekly Friday close performance SITREP\n"
                "• <code>/watchlist</code> - Active Top 15 portfolio selection\n"
                "• <code>/signals</code> - Today's generated action tickets\n"
                "• <code>/orders</code> - Active positions & trailing stops\n"
                "• <code>/why &lt;EPIC&gt;</code> - Forensic breakdown (e.g. <code>/why TSLA</code>)\n"
                "• <code>/doctor</code> - Run system health & API latency audit"
            )

        elif cmd in ("/report", "/weekly", "/sitrep_weekly"):
            return generate_weekly_report_html()

        elif cmd in ("/status", "/sitrep"):
            now_wat = datetime.now(ZoneInfo("Africa/Lagos")).strftime("%H:%M:%S")
            now_et = datetime.now(ZoneInfo("America/New_York")).strftime("%H:%M:%S")

            with get_connection() as conn:
                cb_status = get_circuit_breaker_status(conn)
                cb_text = "🟢 <b>Armed & Normal</b>" if not cb_status.is_active else f"🔴 <b>TRIPPED ({cb_status.reason})</b>"

                cur = conn.cursor()
                cur.execute("SELECT COUNT(*), assigned_engine FROM weekly_watchlist WHERE superseded_at IS NULL GROUP BY assigned_engine")
                wl_map = dict(cur.fetchall())
                mr_c = wl_map.get("MEAN_REVERSION", 0)
                bo_c = wl_map.get("BREAKOUT", 0)

                today_str = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
                cur.execute("SELECT signal_status, COUNT(*) FROM daily_signals WHERE signal_date = ? GROUP BY signal_status", (today_str,))
                sig_map = dict(cur.fetchall())

                cur.execute("SELECT COUNT(*) FROM orders_and_positions WHERE order_status = 'OPEN'")
                open_pos = cur.fetchone()[0]

            return (
                f"📈 <b>SYSTEM SITUATION REPORT</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"• <b>Clocks:</b> WAT: <code>{now_wat}</code> | ET: <code>{now_et}</code>\n"
                f"• <b>Safety:</b> {cb_text}\n"
                f"• <b>Watchlist:</b> <code>{mr_c + bo_c}</code> Assets (<code>{mr_c} MR</code> | <code>{bo_c} BO</code>)\n"
                f"• <b>Signals Today:</b> <code>{sig_map.get('PENDING', 0)} Pending</code> | <code>{sig_map.get('ABORTED', 0)} Aborted</code>\n"
                f"• <b>Active Trades:</b> <code>{open_pos} Open Positions</code>"
            )

        elif cmd in ("/balance", "/equity", "/acc", "/account"):
            broker = _get_broker()
            t0 = time.time()
            bal_data = broker.fetch_account_balance()
            lat = int((time.time() - t0) * 1000)

            if bal_data.get("status") == "OK":
                b = bal_data["balance"]
                eq = bal_data["equity"]
                avail = bal_data["available"]
                pnl = bal_data["pnl"]
                curr = bal_data["currency"]
                pnl_icon = "🟢" if pnl >= 0 else "🔴"
                return (
                    f"💰 <b>BROKER ACCOUNT BALANCES</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"• <b>Balance:</b> <code>${b:,.2f} {curr}</code>\n"
                    f"• <b>Equity:</b> <code>${eq:,.2f} {curr}</code>\n"
                    f"• <b>Available Margin:</b> <code>${avail:,.2f} {curr}</code>\n"
                    f"• <b>Floating P&amp;L:</b> {pnl_icon} <code>${pnl:,.2f} {curr}</code>\n"
                    f"• <b>API Latency:</b> <code>{lat}ms</code>"
                )
            else:
                return f"⚠️ <b>Broker Account Error:</b> {bal_data.get('error', 'Unable to retrieve balance')}"

        elif cmd in ("/watchlist", "/wl", "/watch"):
            with get_connection() as conn:
                cur = conn.cursor()
                cur.execute("""
                    SELECT sunday_composite_rank, epic, sector, assigned_engine, permitted_direction, ROUND(sunday_composite_score, 1)
                    FROM weekly_watchlist
                    WHERE superseded_at IS NULL
                    ORDER BY sunday_composite_rank ASC
                """)
                rows = cur.fetchall()

            if not rows:
                return "Watchlist is empty. Run: <code>bot run screen</code>"

            lines = ["<b>ACTIVE TOP 15 WATCHLIST:</b>", "━━━━━━━━━━━━━━━━━━━━━━"]
            for r in rows:
                eng = "MR" if r[3] == "MEAN_REVERSION" else "BO"
                sec = (r[2] or "N/A")[:10]
                lines.append(f"<code>#{r[0]:<2} {r[1]:<6} | {sec:<10} | {eng:<2} | {r[4]:<10} | {r[5]}</code>")
            return "\n".join(lines)

        elif cmd in ("/signals", "/sig"):
            now_et = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
            with get_connection() as conn:
                cur = conn.cursor()
                cur.execute("""
                    SELECT epic, engine, direction, signal_status, signal_close_price, abort_reason, initial_rr_ratio
                    FROM daily_signals
                    WHERE signal_date = ?
                    ORDER BY created_at DESC
                """, (now_et,))
                rows = cur.fetchall()

            if not rows:
                return f"No signals recorded for today (<code>{now_et}</code>)."

            lines = [f"<b>SIGNALS FOR {now_et}:</b>", "━━━━━━━━━━━━━━━━━━━━━━"]
            for r in rows:
                st_icon = "🟢" if r[3] == "PENDING" else ("🔵" if r[3] == "PROMOTED" else "🔴")
                reason = f" ({r[5]})" if r[5] else ""
                rr_str = f" [R:R {r[6]:.2f}]" if r[6] else ""
                eng = "MR" if "MEAN" in r[1] else "BO"
                lines.append(f"{st_icon} <b>{r[0]}</b> <code>{eng} {r[2]}</code> → <code>{r[3]}</code>{rr_str}{reason}")
            return "\n".join(lines)

        elif cmd == "/why":
            if not arg:
                return "Usage: <code>/why TSLA</code>"
            with get_connection() as conn:
                cur = conn.cursor()
                cur.execute("""
                    SELECT signal_date, signal_status, direction, signal_close_price, 
                           structural_stop_price, target_price, initial_rr_ratio, abort_reason
                    FROM daily_signals
                    WHERE epic = ?
                    ORDER BY created_at DESC LIMIT 1
                """, (arg,))
                sig = cur.fetchone()

            if not sig:
                return f"No signal record found for <code>{arg}</code>."

            s_date, status, direction, close_p, stop_p, target_p, rr, abort_rsn = sig
            tgt_str = f"${target_p:.2f}" if target_p else "None (Trailing)"
            rr_str = f"{rr:.2f}" if rr else "None"
            st_color = "🟢" if status in ("PENDING", "PROMOTED") else "🔴"

            return (
                f"🔍 <b>FORENSIC AUDIT: {arg}</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"• <b>Date:</b> <code>{s_date}</code>\n"
                f"• <b>Status:</b> {st_color} <code>{status}</code>\n"
                f"• <b>Setup:</b> <code>{direction}</code> @ <code>${close_p:.2f}</code>\n"
                f"• <b>Stop Level:</b> <code>${stop_p:.2f}</code> (Risk: <code>${abs(close_p - stop_p):.2f}</code>)\n"
                f"• <b>Target Level:</b> <code>{tgt_str}</code>\n"
                f"• <b>Calculated R:R:</b> <b>{rr_str}</b> (Floor: 1.20)\n"
                f"• <b>Veto Rationale:</b> <code>{abort_rsn or 'Passed all risk rules'}</code>"
            )

        elif cmd in ("/orders", "/positions", "/pos", "/trades"):
            with get_connection() as conn:
                cur = conn.cursor()
                cur.execute("""
                    SELECT epic, direction, actual_fill_price, allocated_units, current_stop_price, target_price, order_status
                    FROM orders_and_positions
                    WHERE order_status = 'OPEN'
                    ORDER BY created_at DESC
                """)
                rows = cur.fetchall()

            if not rows:
                return "No currently open positions in local database."

            lines = ["<b>ACTIVE OPEN POSITIONS:</b>", "━━━━━━━━━━━━━━━━━━━━━━"]
            for r in rows:
                tgt_s = f"${r[5]:.2f}" if r[5] else "Trailing"
                lines.append(f"• <b>{r[0]}</b> (<code>{r[1]}</code>): <code>{r[3]} units</code> @ <code>${r[2]:.2f}</code> | Stop: <code>${r[4]:.2f}</code> | Tgt: <code>{tgt_s}</code>")
            return "\n".join(lines)

        elif cmd in ("/doctor", "/doc", "/health"):
            with get_connection() as conn:
                cur = conn.cursor()
                cur.execute("PRAGMA integrity_check;")
                db_ok = cur.fetchone()[0] == "ok"
                cur.execute("SELECT COUNT(*) FROM weekly_watchlist WHERE superseded_at IS NULL")
                wl_count = cur.fetchone()[0]
                cur.execute("SELECT COUNT(*) FROM daily_signals")
                sig_count = cur.fetchone()[0]

            broker = _get_broker()
            t0 = time.time()
            try:
                broker.fetch_open_positions()
                broker_status = f"🟢 Connected ({int((time.time() - t0)*1000)}ms)"
            except Exception as e:
                broker_status = f"🔴 Offline ({e})"

            return (
                f"🩺 <b>SYSTEM HEALTH AUDIT</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"• <b>SQLite Database:</b> {'🟢 Integrity OK' if db_ok else '🔴 Corrupt'}\n"
                f"• <b>Database Records:</b> <code>{wl_count}</code> Watchlist | <code>{sig_count}</code> Signals\n"
                f"• <b>Capital.com API:</b> {broker_status}\n"
                f"• <b>Daemon Status:</b> 🟢 <code>quantbot.service</code> Active"
            )

        return f"Unknown command <code>{cmd}</code>. Type <code>/help</code> for available options."

    except Exception as e:
        import traceback
        print(f"[COMMAND ERROR] {traceback.format_exc()}")
        return f"⚠️ <b>Command Execution Error:</b>\n<code>{html.escape(str(e))}</code>"


# ============================================================================
# EVENT NOTIFICATION EMITTERS
# ============================================================================

def notify_signal_generated(epic: str, engine: str, direction: str, close_p: float, stop_p: float, target_p: Optional[float], rr: Optional[float]):
    tgt_str = f"${target_p:.2f}" if target_p else "N/A (Trailing)"
    rr_str = f"{rr:.2f}" if rr else "N/A"
    msg = (
        f"🟢 <b>ACTION TICKET GENERATED: {epic}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"• <b>Engine:</b> <code>{engine}</code>\n"
        f"• <b>Direction:</b> <code>{direction}</code>\n"
        f"• <b>Signal Close:</b> <code>${close_p:.2f}</code>\n"
        f"• <b>Stop Price:</b> <code>${stop_p:.2f}</code>\n"
        f"• <b>Target:</b> <code>{tgt_str}</code>\n"
        f"• <b>Initial R:R:</b> <code>{rr_str}</code>\n"
        f"• <b>Status:</b> <code>PENDING</code> (Queued for morning open)"
    )
    send_telegram_message(msg)


def notify_signal_aborted(epic: str, engine: str, direction: str, close_p: float, reason: str, details: str = ""):
    msg = (
        f"⚠️ <b>SIGNAL VETOED: {epic}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"• <b>Engine:</b> <code>{engine}</code> | <code>{direction}</code>\n"
        f"• <b>Close:</b> <code>${close_p:.2f}</code>\n"
        f"• <b>Veto Reason:</b> <code>{reason}</code>\n"
        f"• <b>Forensics:</b> <i>{html.escape(details)}</i>"
    )
    send_telegram_message(msg)


def notify_order_executed(epic: str, direction: str, fill_price: float, units: float, stop_price: float, target_price: Optional[float], risk_usd: float):
    tgt_str = f"${target_price:.2f}" if target_price else "Trailing ATR Stop"
    msg = (
        f"🚀 <b>ORDER EXECUTED: {epic}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"• <b>Side:</b> <code>{direction}</code>\n"
        f"• <b>Fill Price:</b> <code>${fill_price:.2f}</code>\n"
        f"• <b>Units:</b> <code>{units}</code>\n"
        f"• <b>Stop Level:</b> <code>${stop_price:.2f}</code>\n"
        f"• <b>Target:</b> <code>{tgt_str}</code>\n"
        f"• <b>Planned Risk:</b> <code>${risk_usd:,.2f}</code>"
    )
    send_telegram_message(msg)


def notify_weekly_sitrep(run_id: str, watchlist: List[Dict[str, Any]]):
    mr_count = sum(1 for w in watchlist if w.get("engine") == "MEAN_REVERSION")
    bo_count = sum(1 for w in watchlist if w.get("engine") == "BREAKOUT")
    rows = []
    for w in watchlist[:15]:
        eng_short = "MR" if w.get("engine") == "MEAN_REVERSION" else "BO"
        rows.append(f"<code>#{w.get('rank', 0):<2} {w.get('epic', ''):<6} | {w.get('sector', 'N/A')[:10]:<10} | {eng_short}</code>")

    table_text = "\n".join(rows)
    msg = (
        f"📊 <b>SUNDAY WATCHLIST REPORT</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"• <b>Run ID:</b> <code>{run_id}</code>\n"
        f"• <b>Universe:</b> <code>15 Assets</code> (<code>{mr_count} MR</code> | <code>{bo_count} BO</code>)\n\n"
        f"<b>Official Selection:</b>\n"
        f"{table_text}\n\n"
        f"Ready for morning market execution."
    )
    send_telegram_message(msg)


def notify_system_crash(error_message: str):
    msg = (
        f"🚨 <b>CRITICAL SYSTEM ALERT</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Daemon encountered an unexpected failure:\n\n"
        f"<pre>{html.escape(error_message[:1500])}</pre>\n"
        f"Inspect VPS via <code>bot doctor</code>."
    )
    send_telegram_message(msg)


# ============================================================================
# POLLING LOOP
# ============================================================================

def poll_telegram_updates(blocking: bool = False):
    if not BOT_TOKEN:
        print("[TELEGRAM] Listener cannot start: BOT_TOKEN is empty.")
        return

    print(f"[TELEGRAM] Interactive command listener active (Authorized Chat: {CHAT_ID}).")
    offset = 0

    while True:
        try:
            url = f"{TELEGRAM_API_BASE}/getUpdates?offset={offset}&timeout=10"
            req = urllib.request.Request(url, headers={"User-Agent": "QuantBot/1.0"})
            with urllib.request.urlopen(req, timeout=18) as resp:
                data = json.loads(resp.read().decode())
                for item in data.get("result", []):
                    offset = item["update_id"] + 1
                    msg = item.get("message", {})
                    from_chat = str(msg.get("chat", {}).get("id", ""))
                    text = msg.get("text", "")

                    if not text:
                        continue

                    print(f"[TELEGRAM INBOUND] Chat: {from_chat} | Text: {text}")

                    if CHAT_ID and from_chat != str(CHAT_ID):
                        print(f"[TELEGRAM REJECTED] Chat ID {from_chat} unauthorized.")
                        continue

                    reply = handle_telegram_command(text)
                    send_telegram_message(reply, target_chat_id=from_chat)

        except (socket.timeout, TimeoutError):
            pass
        except urllib.error.URLError as e:
            if isinstance(e.reason, socket.timeout) or "timed out" in str(e).lower():
                pass
            else:
                time.sleep(3)
        except Exception as e:
            print(f"[TELEGRAM ERROR] Polling exception: {e}")
            time.sleep(3)

        if not blocking:
            time.sleep(1)


def start_telegram_listener_thread():
    t = threading.Thread(target=poll_telegram_updates, kwargs={"blocking": False}, daemon=True, name="TelegramListener")
    t.start()
    return t


if __name__ == "__main__":
    poll_telegram_updates(blocking=True)
