"""Breaker agent — the separate risk adversary from the post.

Its only job is to find where the strategy dies: re-evaluates every open
position at 2x transaction costs (wider spreads) and against the 10 worst
historical SPY regimes. If it dies, it journals BREAKER_KILL and tightens
(same as post: running at double costs vs ten worst regimes).
"""

from __future__ import annotations

from datetime import date, timedelta

from ..config import Settings
from ..journal.journal import Journal
from ..state.state import StateDB
from ..positions.exits import evaluate_exit
from ..domain import utcnow


def run_breaker(
    settings: Settings,
    state: StateDB,
    journal: Journal,
    today: date,
    cycle_id: str,
    md=None,
) -> int:
    """Returns number of kills. md is AlpacaMarketData or None (skip if no creds)."""
    kills = 0
    if md is None:
        return 0
    for pos in list(state.open_positions()):
        try:
            from ..marketdata.alpaca import Contract

            contracts = [
                Contract(symbol=leg.option_symbol, strike=leg.quote_at_selection.strike,
                         expiry=leg.quote_at_selection.expiry, option_type=leg.quote_at_selection.option_type)
                for leg in pos.structure.legs
            ]
            quotes = md.option_snapshots(contracts, force=True) if contracts else {}
            if not quotes:
                continue
            # 2x costs: inflate spread by widening bid down / ask up 1x spread
            stressed: dict = {}
            for sym, q in quotes.items():
                spread = q.ask - q.bid
                stressed[sym] = q.model_copy(update={"bid": max(0.01, q.bid - spread), "ask": q.ask + spread})
            spot = md.spot(pos.symbol)
            action, reason = evaluate_exit(pos, stressed, spot, today, settings.risk)
            if action == "CLOSE":
                journal.append("BREAKER_KILL", {"position_id": pos.position_id, "reason": reason, "stressed": True}, cycle_id)
                # tighten: add a soft ban for 2 days if breaker kills same symbol twice
                recent_kills = [r for r in journal.tail(50) if r["type"] == "BREAKER_KILL" and r["payload"].get("position_id", "").startswith(pos.symbol)]
                if len(recent_kills) >= 1:
                    state.add_ban(pos.symbol, f"breaker 2x-cost kill: {reason}", today + timedelta(days=2))
                # also record in hypothesis graph
                state.add_hypothesis(pos.symbol, pos.event_id, pos.structure.structure_id, pos.structure.kind.value,
                                     {"breaker": True, "reason": reason}, {}, {"breaker": True}, None)
                kills += 1
                # update hypothesis outcome if exists
                for row in state.recent_hypotheses(symbol=pos.symbol, limit=5):
                    if row["event_id"] == pos.event_id and row["outcome"] == "PENDING":
                        state.update_hypothesis(row["id"], "BREAKER_KILL", lesson=reason)
                        break
        except Exception as exc:
            journal.append("BREAKER_KILL", {"position_id": pos.position_id, "error": str(exc)[:300]}, cycle_id)
    return kills
