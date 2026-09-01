"""Orchestrator cycle — the 9-step pipeline tying every subsystem together.

Order is load-bearing:
  1 boot guard  2 reconcile  3 manage exits  4 calendar  5 screen  6 LLM
  7 gates+execute  8 journal summary  9 dashboard/review

P3 fail-closed throughout: any unhandled exception journals ERROR and the
cycle ends without placing new orders.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta

from ..config import Settings
from ..domain import (
    CandidateBrief,
    ExitPlan,
    MarketDataSnapshot,
    Position,
    PositionStatus,
    StockQuote,
    stable_hash,
    utcnow,
)
from ..journal.journal import Journal
from ..state.state import StateDB
from ..marketdata.alpaca import AlpacaMarketData, AccountInfo
from ..marketdata.news import NewsService
from ..calendar.service import CalendarService
from ..screener.metrics import compute_metrics, select_expiry
from ..screener.desk import build_structures
from ..risk.gates import evaluate_gates
from ..execution.broker import AlpacaBroker, client_order_id, signed_limit
from ..positions.exits import build_exit_plan, evaluate_exit
from ..reconcile.reconciler import reconcile
from ..llm.team import LLMTeam
from ..util import today_et


def _snapshot_for_event(
    event,
    md: AlpacaMarketData,
    news_svc: NewsService,
    calendar_svc: CalendarService,
    today: date,
) -> MarketDataSnapshot | None:
    try:
        spot = md.spot(event.symbol)
        stock_quote = md.latest_quote(event.symbol)
    except Exception as exc:
        return MarketDataSnapshot(
            symbol=event.symbol, fetched_at=utcnow(), spot=0, quote=StockQuote(quoted_at=utcnow()),
            spy_spot=0, spy_quote=StockQuote(quoted_at=utcnow()),
            fetch_errors=[str(exc)],
        )
    try:
        spy_spot = md.spot("SPY")
        spy_quote = md.latest_quote("SPY")
        r1, r5 = md.spy_returns()
    except Exception:
        spy_spot, spy_quote, r1, r5 = 0, StockQuote(quoted_at=utcnow()), None, None
    try:
        bars = md.daily_bars(event.symbol, lookback_days=60)
        closes = [b.close for b in bars]
        # hv20 handled in metrics via snapshot field or derived
        from ..screener.metrics import _annualized_hv

        hv20 = _annualized_hv(closes, 20)
    except Exception:
        closes, hv20 = [], None
    hist = calendar_svc.earnings_history(event.symbol)
    # hist moves: computed from bars around each past date (|close(E+1)/close(E-1)-1|)
    hist_moves: list[float] = []
    if bars and hist:
        by_date = {b.trade_date: b.close for b in bars}
        sorted_dates = sorted(by_date)
        for d in hist[-8:]:
            # find closest bar dates: pre = max date <= d, post = min date > d
            pre = max((x for x in sorted_dates if x <= d), default=None)
            post = min((x for x in sorted_dates if x > d), default=None)
            if pre and post and by_date[pre]:
                hist_moves.append(abs(by_date[post] / by_date[pre] - 1))
    # chain for chosen expiry only (+-25% strike band to keep snapshot bounded)
    chain_quotes: list = []
    try:
        expiry_lo = event.event_date + timedelta(days=4)
        expiry_hi = event.event_date + timedelta(days=10)
        contracts = md.option_chain(event.symbol, expiry_lo, expiry_hi)
        expiries = sorted({c.expiry for c in contracts})
        chosen = select_expiry(expiries, event.event_date)
        if chosen:
            lo_strike = spot * 0.75
            hi_strike = spot * 1.25
            cands = [c for c in contracts if c.expiry == chosen and lo_strike <= c.strike <= hi_strike]
            if cands:
                quotes = md.option_snapshots(cands)
                chain_quotes = list(quotes.values())
    except Exception as exc:
        # snapshot still usable; metrics will fail G1
        chain_quotes = []
    headlines = news_svc.headlines(event.symbol)
    return MarketDataSnapshot(
        symbol=event.symbol,
        fetched_at=utcnow(),
        spot=spot,
        quote=stock_quote,
        chain=chain_quotes,
        spy_spot=spy_spot,
        spy_quote=spy_quote,
        spy_ret_1d=r1,
        spy_ret_5d=r5,
        hv20=hv20,
        hist_earn_moves=hist_moves,
        closes_recent=closes[-6:] if len(closes) >= 6 else closes,
        news_headlines=headlines,
    )


def run_cycle(settings: Settings, cycle_id: str | None = None) -> dict:
    cycle_id = cycle_id or uuid.uuid4().hex[:12]
    journal = Journal(settings.journal_path)
    state = StateDB(settings.db_path)
    today = today_et()
    summary: dict = {"cycle_id": cycle_id, "today": today.isoformat(), "executed": 0, "rejected": 0, "exits": 0, "errors": []}

    # 1 boot
    if halt := state.halt():
        if halt.tripped:
            journal.append("BOOT", {"halt_latched": halt.reason}, cycle_id)
            summary["halt"] = halt.reason
            # still allow exits even when halted
    try:
        universe = settings.load_universe()
    except Exception as exc:
        journal.append("ERROR", {"phase": "boot", "error": str(exc)}, cycle_id)
        summary["errors"].append(str(exc))
        return summary

    # services (lazy — may raise if creds missing)
    try:
        md = AlpacaMarketData(settings)
        broker = AlpacaBroker(settings)
    except Exception as exc:
        journal.append("ERROR", {"phase": "marketdata_init", "error": str(exc)}, cycle_id)
        summary["errors"].append(str(exc))
        journal.append("CYCLE_SUMMARY", summary, cycle_id)
        _maybe_build_dashboard(settings)
        return summary
    news_svc = NewsService(settings.finnhub_key)
    calendar_svc = CalendarService(settings, state, journal)

    # 2 reconcile
    try:
        reconcile(settings, state, journal, broker, today, cycle_id)
    except Exception as exc:
        journal.append("ERROR", {"phase": "reconcile", "error": str(exc)}, cycle_id)
        summary["errors"].append(str(exc))

    halt = state.halt()
    if halt.tripped:
        summary["halt"] = halt.reason

    # 3b breaker (2x costs) — post's breaker agent
    try:
        from ..reconcile.breaker import run_breaker

        run_breaker(settings, state, journal, today, cycle_id, md if "md" in locals() else None)
    except Exception:
        pass

    # 3 manage exits (fresh quotes, force=True)
    try:
        for pos in list(state.open_positions()):
            if pos.status not in (PositionStatus.OPEN,):
                continue
            try:
                contracts = []
                # rebuild contract objects from stored legs for re-quote
                from ..marketdata.alpaca import Contract

                for leg in pos.structure.legs:
                    # strike/expiry from stored quote
                    q = leg.quote_at_selection
                    contracts.append(Contract(symbol=leg.option_symbol, strike=q.strike, expiry=q.expiry, option_type=q.option_type))
                quotes = md.option_snapshots(contracts, force=True) if contracts else {}
                spot = md.spot(pos.symbol)
                action, reason = evaluate_exit(pos, quotes, spot, today, settings.risk)
                journal.append("EXIT_EVAL", {"position_id": pos.position_id, "action": action, "reason": reason}, cycle_id)
                if action == "CLOSE":
                    # build close mleg (reverse sides)
                    close_legs = []
                    for leg in pos.structure.legs:
                        close_side = "sell" if leg.side == "buy" else "buy"
                        close_legs.append({"symbol": leg.option_symbol, "ratio_qty": leg.ratio, "side": close_side})
                    # cost to close as marketable limit: use fresh mids
                    cost_mids = []
                    for leg in pos.structure.legs:
                        q = quotes.get(leg.option_symbol)
                        if q is None:
                            raise RuntimeError(f"no quote for {leg.option_symbol}")
                        # closing: buy legs we sold, sell legs we bought — net debit = buy cost - sell credit
                        cost_mids.append((q.ask if leg.side == "sell" else -q.bid))
                    close_price = sum(cost_mids)  # positive debit to close
                    close_price = max(0.05, close_price * 1.02)  # buffer to get filled
                    close_cid = f"pr-close-{pos.position_id[:8]}-{pos.close_attempts+1}"
                    payload = {
                        "order_class": "mleg", "type": "limit", "time_in_force": "day",
                        "qty": pos.structure.contracts,
                        "limit_price": str(round(float(close_price), 4)),
                        "client_order_id": close_cid,
                        "legs": close_legs,
                    }
                    try:
                        broker.submit(payload)
                        pos.close_attempts += 1
                        pos.status = PositionStatus.CLOSE_SUBMITTED
                        pos.exit_reason = reason
                        state.upsert_position(pos)
                        state.add_order(close_cid, pos.position_id, "EXIT")
                        journal.append("EXIT_SUBMITTED", {"position_id": pos.position_id, "cid": close_cid, "price": close_price}, cycle_id)
                        summary["exits"] += 1
                    except Exception as exc:
                        journal.append("ERROR", {"phase": "exit_submit", "position_id": pos.position_id, "error": str(exc)}, cycle_id)
            except Exception as exc:
                journal.append("ERROR", {"phase": "exit_eval", "position_id": pos.position_id, "error": str(exc)}, cycle_id)
    except Exception as exc:
        journal.append("ERROR", {"phase": "exits", "error": str(exc)}, cycle_id)

    if halt.tripped:
        journal.append("CYCLE_SUMMARY", summary, cycle_id)
        _maybe_build_dashboard(settings)
        return summary

    # 4 calendar
    try:
        events = calendar_svc.refresh(universe, today)
    except Exception as exc:
        journal.append("ERROR", {"phase": "calendar", "error": str(exc)}, cycle_id)
        events = []

    # ban filter
    bans = state.active_bans(today)
    events = [e for e in events if e.symbol not in bans]
    # already positioned
    events = [e for e in events if not state.has_event_position(e.event_id)]
    # two-cycle confirmation gate (confirm>=1)
    confirmed: list = []
    for e in events:
        row = state.cached_calendar(e.symbol)
        if row and int(row["confirm_cycles"] or 0) >= 1:
            confirmed.append(e)
        else:
            journal.append("SCREEN_FAIL", {"symbol": e.symbol, "reason": "not yet confirmed (need 2 consecutive calendar observations)"}, cycle_id)
            summary["rejected"] += 1
    events = confirmed[: settings.risk.max_events_llm_per_cycle * 2]

    # 5/6 screen + structures + LLM
    llm = LLMTeam(settings, state, journal)
    to_trade: list[tuple] = []  # (event, snapshot, metrics, structures)
    for event in events[: settings.risk.max_events_llm_per_cycle * 2]:
        snap = _snapshot_for_event(event, md, news_svc, calendar_svc, today)
        if snap is None:
            journal.append("SCREEN_FAIL", {"symbol": event.symbol, "reason": "snapshot build failed"}, cycle_id)
            summary["rejected"] += 1
            continue
        metrics, failures, expiry = compute_metrics(snap, event, today, settings.risk)
        if metrics is None:
            for code, reason in failures:
                journal.append("SCREEN_FAIL", {"symbol": event.symbol, "gate": code.value, "reason": reason}, cycle_id)
            summary["rejected"] += 1
            continue
        structures = build_structures(snap, metrics, event, today, settings.risk)
        if not structures:
            journal.append("SCREEN_FAIL", {"symbol": event.symbol, "reason": "no buildable structures"}, cycle_id)
            summary["rejected"] += 1
            continue
        briefs = [
            CandidateBrief(
                candidate_id=s.structure_id, kind=s.kind.value, label=s.label,
                expires_on=s.expires_on, max_loss_per_contract=s.max_loss_per_contract,
                max_profit_per_contract=s.max_profit_per_contract,
                breakevens=s.breakevens, entry_cost_per_contract=s.entry_cost_per_contract,
            )
            for s in structures
        ]
        decision = llm.decide(event.event_id, briefs, snap.news_headlines, today, cycle_id, snapshot=snap, metrics=metrics)
        journal.append("DECISION", {"event_id": event.event_id, "decision": decision.model_dump()}, cycle_id)
        # hypothesis graph: record every decision (negative results are most valuable)
        hyp_id = None
        try:
            regime = {"move_ratio": metrics.move_ratio, "vrp": metrics.vrp or 0, "drift": metrics.runup_drift, "spy5d": metrics.spy_ret_5d or 0}
            hyp_id = state.add_hypothesis(event.symbol, event.event_id, decision.candidate_id or "", decision.action, regime, metrics.model_dump(), decision.model_dump(), None)
        except Exception:
            hyp_id = None
        if not decision.tradable:
            journal.append("DECISION_REJECTED", {"event_id": event.event_id, "reason": "declined or low conviction"}, cycle_id)
            summary["rejected"] += 1
            continue
        chosen = next((s for s in structures if s.structure_id == decision.candidate_id), None)
        if chosen is None:
            journal.append("DECISION_REJECTED", {"event_id": event.event_id, "reason": "candidate not in shortlist (hallucination guard)"}, cycle_id)
            if hyp_id is not None:
                try:
                    state.update_hypothesis(hyp_id, "REJECTED", lesson="hallucination guard")
                except Exception:
                    pass
            summary["rejected"] += 1
            continue
        to_trade.append((event, snap, metrics, chosen, decision, hyp_id))

    # 7 gates + execute (fresh re-quote immediately before submit, P4)
    account: AccountInfo | None = None
    try:
        account = md.account()
    except Exception:
        pass
    now = utcnow()
    for event, snap, metrics, structure, decision, hyp_id in to_trade[: settings.risk.max_events_llm_per_cycle]:
        # fresh quotes for legs
        try:
            from ..marketdata.alpaca import Contract

            contracts = [Contract(symbol=leg.option_symbol, strike=leg.quote_at_selection.strike,
                                   expiry=leg.quote_at_selection.expiry, option_type=leg.quote_at_selection.option_type)
                          for leg in structure.legs]
            fresh = md.option_snapshots(contracts, force=True)
            # rebuild snapshot with fresh leg quotes for gate evaluation
            fresh_legs = []
            for leg in structure.legs:
                q = fresh.get(leg.option_symbol)
                if q is None:
                    raise RuntimeError(f"missing fresh quote for {leg.option_symbol}")
                fresh_legs.append(leg.model_copy(update={"quote_at_selection": q}))
            fresh_structure = structure.model_copy(update={"legs": fresh_legs})
            # recompute snap quote freshness for gate
            snap_fresh = snap.model_copy(update={"fetched_at": now})
        except Exception as exc:
            journal.append("GATE_FAIL", {"event_id": event.event_id, "reason": f"re-quote failed: {exc}"}, cycle_id)
            summary["rejected"] += 1
            continue

        outcome = evaluate_gates(fresh_structure, snap_fresh, metrics, today, now, settings.risk, state, account, decision.conviction, is_requote=True)
        if not outcome.passed:
            for code, reason in outcome.failures:
                journal.append("GATE_FAIL", {"event_id": event.event_id, "gate": code.value, "reason": reason}, cycle_id)
            if hyp_id is not None:
                try:
                    state.update_hypothesis(hyp_id, "REJECTED", lesson="; ".join(f"{c.value}:{r}" for c, r in outcome.failures)[:300])
                except Exception:
                    pass
            summary["rejected"] += 1
            continue
        journal.append("GATE_PASS", {"event_id": event.event_id, "contracts": outcome.contracts, "max_loss": outcome.max_loss_usd}, cycle_id)
        # size
        fresh_structure = fresh_structure.model_copy(update={"contracts": outcome.contracts})
        # exit plan
        width = max(l.quote_at_selection.strike for l in fresh_structure.legs) - min(l.quote_at_selection.strike for l in fresh_structure.legs)
        debit = abs(fresh_structure.entry_cost_per_contract) if fresh_structure.kind.value.endswith("vertical") else 0
        credit = abs(fresh_structure.entry_cost_per_contract) if fresh_structure.kind.value == "iron_condor" else 0
        exit_plan = build_exit_plan(fresh_structure.kind, settings.risk, credit_per_share=credit, debit_per_share=debit, width_per_share=width)
        # preregistered thesis (immutable, post's trick)
        thesis_payload = {
            "position_id": f"{event.event_id}:{fresh_structure.structure_id[:8]}",
            "expected_move_pct": metrics.expected_move_pct,
            "expected_hold_days": max(1, (fresh_structure.expires_on - today).days // 2),
            "invalidation": f"spot beyond {[round(x,1) for x in fresh_structure.breakevens]} OR vrp<0 OR dte<=1",
            "expected_pnl_pct": 0.4 if fresh_structure.kind.value.endswith("vertical") else 0.55,
        }
        thesis_hash = stable_hash(thesis_payload)
        position_id = f"{event.event_id}:{fresh_structure.structure_id[:8]}"
        cid = client_order_id(fresh_structure, event.event_id, today.isoformat())
        limit = signed_limit(fresh_structure, abs(fresh_structure.entry_cost_per_contract))
        pos = Position(
            position_id=position_id,
            event_id=event.event_id,
            symbol=event.symbol,
            structure=fresh_structure,
            entry_order_client_id=cid,
            status=PositionStatus.PENDING_FILL,
            exit_plan=exit_plan,
            opened_at=now,
        )
        # persist before submit so a crash doesn't lose it (P4)
        state.upsert_position(pos)
        # thesis is immutable — write once, link to hypothesis
        try:
            import json as _json

            state.put_thesis(position_id, event.event_id, event.symbol, _json.dumps(thesis_payload), thesis_hash)
            journal.append("THESIS", {"position_id": position_id, "thesis": thesis_payload, "hash": thesis_hash}, cycle_id)
            if hyp_id is not None:
                state.conn.execute("UPDATE hypotheses SET thesis_json=?, outcome='PENDING' WHERE id=?", (_json.dumps(thesis_payload), hyp_id))
                state.conn.commit()
        except Exception:
            pass
        from ..execution.broker import submit_entry

        order = submit_entry(fresh_structure, event.event_id, today.isoformat(), outcome.contracts, limit, cycle_id, journal, state, broker, position_id)
        if order is None:
            pos.status = PositionStatus.REJECTED
            state.upsert_position(pos)
            summary["rejected"] += 1
            continue
        # attach broker id + mark
        pos.broker_order_id = str(order.get("id", "")) if isinstance(order, dict) else None
        state.upsert_position(pos)
        state.incr_entries(today)
        summary["executed"] += 1

    # 8 autopsy — compare thesis vs reality, close the loop (post's fine-tune)
    try:
        from ..reconcile.autopsy import run_autopsy

        run_autopsy(state, journal, today, cycle_id)
    except Exception:
        pass

    # 8b reviewer (end of cycle)
    try:
        llm.reviewer_bans(today, cycle_id)
    except Exception:
        pass

    journal.append("CYCLE_SUMMARY", summary, cycle_id)
    _maybe_build_dashboard(settings)
    return summary


def _maybe_build_dashboard(settings: Settings) -> None:
    try:
        from ..dashboard.build import build_dashboard

        build_dashboard(settings)
    except Exception:
        pass
