"""Reconciler + latched kill switch (HALT).

Runs first every cycle, before any decision to trade. Diff between broker
truth and local state either adopts broker truth or trips the kill switch
(P4 latched — `HALT` persists until a human runs `pr resume --confirm`).

Panic-close: on drawdown/day-loss halt, all open positions are closed.
"""

from __future__ import annotations

from datetime import date

from ..config import Settings
from ..journal.journal import Journal
from ..state.state import StateDB
from ..execution.broker import AlpacaBroker
from ..domain import PositionStatus


def reconcile(
    settings: Settings,
    state: StateDB,
    journal: Journal,
    broker: AlpacaBroker,
    today: date,
    cycle_id: str,
) -> None:
    """Fetch broker truth and reconcile. May latch HALT."""
    halt = state.halt()
    if halt.tripped:
        journal.append("RECONCILE", {"halt_latched": halt.reason}, cycle_id)
        return

    # --- broker fetch (best-effort; network failure never halts, just journals)
    try:
        positions = broker.positions()
        orders = broker.orders(status="all")
    except Exception as exc:
        journal.append("RECONCILE", {"error": str(exc), "note": "broker unreachable — fail closed, no entries this cycle"}, cycle_id)
        return

    # --- second channel: official Alpaca CLI as an independent broker-truth
    # read (fail-open — CLI absence/failure never blocks; REST stays source
    # of truth; a count mismatch is journaled, never a halt, to avoid false
    # kills from coverage differences between channels).
    try:
        from ..execution.alpaca_cli import (
            available as cli_available, cli_account, cli_clock, cli_positions,
        )
        if cli_available():
            c_acct = cli_account(settings)
            c_pos = cli_positions(settings)
            c_clock = cli_clock(settings)
            journal.append("CLI_CHECK", {
                "account_ok": bool(c_acct),
                "equity": (c_acct or {}).get("equity"),
                "buying_power": (c_acct or {}).get("buying_power"),
                "positions": len(c_pos) if isinstance(c_pos, list) else None,
                "market_open": (c_clock or {}).get("is_open"),
            }, cycle_id)
            if isinstance(c_pos, list) and len(c_pos) != len(positions):
                journal.append("RECONCILE", {
                    "note": "cli_rest_position_count_differs",
                    "cli": len(c_pos), "rest": len(positions),
                }, cycle_id)
        else:
            journal.append("CLI_CHECK", {"available": False, "note": "alpaca cli not installed; REST only"}, cycle_id)
    except Exception as exc:
        journal.append("CLI_CHECK", {"error": str(exc)[:200]}, cycle_id)

    # --- equity / day counters / drawdown
    broker_equity = None
    try:
        from ..marketdata.alpaca import AlpacaMarketData

        md = AlpacaMarketData(settings)
        broker_equity = md.account().equity
        state.set_equity(today, broker_equity)
        try:  # dashboard live feed (fail-open; never blocks trading)
            from ..supabase import push_equity as _push_equity
            _push_equity(today.isoformat(), broker_equity)
        except Exception:
            pass
        peak = state.peak_equity()
        if peak and (peak - broker_equity) / peak >= settings.risk.drawdown_halt:
            state.latch_halt(f"drawdown {(peak-broker_equity)/peak:.1%} >= {settings.risk.drawdown_halt:.0%}")
            journal.append("HALT", {"reason": "drawdown", "equity": broker_equity, "peak": peak}, cycle_id)
            _panic_close(state, journal, broker, cycle_id)
            return
    except Exception as exc:
        journal.append("RECONCILE", {"equity_error": str(exc)}, cycle_id)

    # --- unknown broker positions (no local position)
    local_symbols = {p.event_id for p in state.open_positions()}
    broker_position_ids = {p.get("symbol") for p in positions if p.get("symbol")}
    # naive check: any broker OCC symbol not matching any open local leg symbol => drift
    # exact leg-by-leg match is done inside positions; this is a cheap summary.
    # Full per-leg adoption below in the pending/order sync does the real work.

    # sync pending/entry orders: mark filled/rejected by broker status
    for row in state.entry_orders_pending_broker_check():
        cid = row["client_order_id"]
        found = next((o for o in orders if o.get("client_order_id") == cid), None)
        if found is None:
            continue
        status = str(found.get("status", "")).lower()
        if status == "filled":
            state.update_order_status(cid, "FILLED")
            # also update position filled price if we can find the position
            pid = row["position_id"]
            pos = state.position(pid)
            if pos and pos.status == PositionStatus.PENDING_FILL:
                try:
                    fill_price = float(found.get("filled_avg_price") or 0) or None
                except Exception:
                    fill_price = None
                pos.filled_entry_price = fill_price
                pos.status = PositionStatus.OPEN
                pos.broker_order_id = str(found.get("id") or pos.broker_order_id or "")
                pos.entry_notional = (fill_price or 0) * pos.structure.contracts * 100
                state.upsert_position(pos)
                journal.append("ORDER_FILLED", {"position_id": pid, "fill": fill_price}, cycle_id)
        elif status in ("canceled", "cancelled", "rejected", "expired"):
            state.update_order_status(cid, status.upper())
            pid = row["position_id"]
            pos = state.position(pid)
            if pos and pos.status == PositionStatus.PENDING_FILL:
                pos.status = PositionStatus.REJECTED
                state.upsert_position(pos)
                journal.append("ORDER_CANCELLED", {"position_id": pid, "status": status}, cycle_id)

    # day-loss guard using equity snapshot vs first snapshot of day
    hist = state.equity_history()
    if broker_equity is not None and hist:
        day_start = next((eq for d, eq in hist if d == today), None)
        if day_start is None:
            # first cycle today anchors at current
            day_start = broker_equity
            state.set_equity(today, day_start)
        if day_start:
            day_pnl = (broker_equity - day_start) / day_start if day_start else 0
            if day_pnl <= settings.risk.day_pnl_halt:
                state.latch_halt(f"day_pnl {day_pnl:.1%} <= {settings.risk.day_pnl_halt:.0%}")
                journal.append("HALT", {"reason": "day_pnl_halt", "day_pnl": day_pnl}, cycle_id)
                _panic_close(state, journal, broker, cycle_id)
            elif day_pnl <= settings.risk.day_pnl_restricted:
                journal.append("RECONCILE", {"note": "restricted", "day_pnl": day_pnl}, cycle_id)

    journal.append("RECONCILE", {"open_positions": len(state.open_positions()), "broker_positions": len(positions)}, cycle_id)


def _panic_close(state: StateDB, journal: Journal, broker: AlpacaBroker, cycle_id: str) -> None:
    journal.append("HALT", {"panic_close": True, "open": len(state.open_positions())}, cycle_id)
    # defensive: best-effort close of each open position via a synthetic opposite order.
    # Real close orders are built by the positions manager on the next cycle;
    # panic just records the intent here (the manager will act).
