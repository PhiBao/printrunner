"""Operator waiver entry — a safety-floored, fully audited override channel.

Context: the autonomous gates may find no compliant setup for sessions at a
time (e.g. rich EM + negative drift satisfies neither the RUNUP nor the
CRUSH family). This module lets a human operator direct ONE entry while
keeping every safety property of the normal path:

  * ONLY edge gates G9/G10 may be waived. Safety gates G1-G8 must ALL pass —
    any G1-G8 failure refuses, no exceptions.
  * Identical machinery: fresh re-quote, conviction sizing, mleg builder,
    deterministic client_order_id, persist-before-submit, preregistered
    thesis, exit plan, reconcile-managed exits afterwards.
  * Everything journaled as OPERATOR_OVERRIDE with the waived codes,
    the human rationale, and the safety-gate evidence.

This is an accountability feature, not a backdoor: a waiver is louder in
the audit trail than a normal entry, and the submission must disclose it.
"""

from __future__ import annotations

from datetime import date

from ..config import Settings
from ..domain import (
    EarningsEvent,
    GateCode,
    Position,
    PositionStatus,
    StructureKind,
    utcnow,
)
from ..journal.journal import Journal
from ..state.state import StateDB

# Edge gates only. G1-G8 (data, staleness, sanity, structure, expiry,
# liquidity, budget, market shock) are NEVER waivable.
WAIVABLE = {GateCode.G9, GateCode.G10}


def parse_waived(spec: str) -> set[GateCode]:
    """Parse 'G9,G10' (short or full enum values) into GateCodes."""
    out: set[GateCode] = set()
    for raw in (spec or "").split(","):
        token = raw.strip().upper()
        if not token:
            continue
        for code in GateCode:
            if token in (code.name.upper(), code.value.upper()):
                out.add(code)
                break
        else:
            raise ValueError(f"unknown gate code: {raw!r}")
    return out


def run_waiver(
    settings: Settings,
    symbol: str,
    event_date: date,
    kind: StructureKind,
    waived: set[GateCode],
    conviction: int,
    reason: str,
) -> dict:
    """Execute a waived entry. Returns a summary dict (with 'refused' key on
    refusal — refusals never submit orders)."""
    journal = Journal(settings.journal_path)
    state = StateDB(settings.db_path)
    cycle_id = f"waiver-{utcnow().strftime('%H%M%S')}"

    non_waivable = set(waived) - WAIVABLE
    if non_waivable:
        journal.append("OPERATOR_OVERRIDE", {
            "symbol": symbol, "refused": True,
            "reason": f"non-waivable gates requested: {sorted(c.value for c in non_waivable)}",
        }, cycle_id)
        return {"refused": f"only G9/G10 are waivable, got {[c.value for c in non_waivable]}"}

    from ..marketdata.alpaca import AlpacaMarketData
    from ..marketdata.news import NewsService
    from ..calendar.service import CalendarService
    from ..orchestrator.cycle import _snapshot_for_event
    from ..screener.metrics import compute_metrics
    from ..screener.desk import build_structures
    from ..risk.gates import evaluate_gates
    from ..execution.broker import AlpacaBroker, client_order_id, signed_limit, submit_entry
    from ..positions.exits import build_exit_plan
    from ..domain import stable_hash
    from ..util import today_et

    today = today_et()
    try:
        md = AlpacaMarketData(settings)
        broker = AlpacaBroker(settings)
    except Exception as exc:
        return {"refused": f"broker init: {exc}"}
    news_svc = NewsService(settings.finnhub_key)
    calendar_svc = CalendarService(settings, state, journal)

    event = EarningsEvent(symbol=symbol, event_date=event_date, timing="UNSPECIFIED",  # type: ignore[arg-type]
                          source="operator", captured_at=utcnow())  # type: ignore[arg-type]
    if state.has_event_position(event.event_id):
        return {"refused": "event already has a position"}
    snap = _snapshot_for_event(event, md, news_svc, calendar_svc, today)
    if snap is None:
        return {"refused": "snapshot build failed"}
    metrics, failures, _ = compute_metrics(snap, event, today, settings.risk)
    if metrics is None:
        journal.append("OPERATOR_OVERRIDE", {"symbol": symbol, "refused": True,
                                             "reason": "metrics failed (G1/G5 safety, not waivable)",
                                             "failures": [f"{c.value}:{r}" for c, r in failures]}, cycle_id)
        return {"refused": f"metrics failed: {failures}"}
    structures = build_structures(snap, metrics, event, today, settings.risk)
    structure = next((s for s in structures if s.kind == kind), None)
    if structure is None:
        journal.append("OPERATOR_OVERRIDE", {"symbol": symbol, "refused": True,
                                             "reason": f"desk built no {kind.value}",
                                             "built": [s.kind.value for s in structures]}, cycle_id)
        return {"refused": f"desk built no {kind.value}"}

    # Fresh re-quote (P4), exactly like the normal path.
    from ..marketdata.alpaca import Contract
    now = utcnow()
    try:
        contracts = [Contract(symbol=leg.option_symbol, strike=leg.quote_at_selection.strike,
                              expiry=leg.quote_at_selection.expiry,
                              option_type=leg.quote_at_selection.option_type)
                     for leg in structure.legs]
        fresh = md.option_snapshots(contracts, force=True)
        fresh_legs = []
        for leg in structure.legs:
            q = fresh.get(leg.option_symbol)
            if q is None:
                raise RuntimeError(f"missing fresh quote for {leg.option_symbol}")
            fresh_legs.append(leg.model_copy(update={"quote_at_selection": q}))
        structure = structure.model_copy(update={"legs": fresh_legs})
        snap_fresh = snap.model_copy(update={"fetched_at": now})
    except Exception as exc:
        return {"refused": f"re-quote failed: {exc}"}

    try:
        account = md.account()
    except Exception:
        account = None
    outcome = evaluate_gates(structure, snap_fresh, metrics, today, now,
                             settings.risk, state, account, conviction, is_requote=True)
    failed_codes = {c for c, _ in outcome.failures}
    unwaived = failed_codes - set(waived)
    if unwaived:
        journal.append("OPERATOR_OVERRIDE", {
            "symbol": symbol, "structure": structure.label, "refused": True,
            "reason": "non-waived gates failed (safety floor held)",
            "failed": [f"{c.value}:{r}" for c, r in outcome.failures],
            "waived": sorted(c.value for c in waived),
        }, cycle_id)
        return {"refused": f"safety floor held: {[c.value for c in unwaived]}"}

    # Size identically to G7 (conviction scales down only).
    mult = settings.risk.conviction_multiplier(conviction)
    if mult == 0:
        return {"refused": f"conviction {conviction} not tradable"}
    per_contract = structure.max_loss_per_contract
    qty = int(settings.risk.max_loss_per_event_usd * mult // per_contract) if per_contract > 0 else 0
    if qty < 1:
        return {"refused": "budget too small even at this conviction"}
    agg = state.aggregate_open_risk()
    if agg + per_contract * qty > settings.risk.max_aggregate_open_risk_usd + 1e-6:
        return {"refused": "aggregate cap would breach"}
    if len(state.open_positions()) >= settings.risk.max_concurrent_events:
        return {"refused": "concurrent cap reached"}
    if state.entries_today(today) >= settings.risk.max_entries_per_day:
        return {"refused": "daily entries cap reached"}

    structure = structure.model_copy(update={"contracts": qty})
    width = max(l.quote_at_selection.strike for l in structure.legs) - min(l.quote_at_selection.strike for l in structure.legs)
    debit = abs(structure.entry_cost_per_contract) if structure.kind.value.endswith("vertical") else 0
    credit = abs(structure.entry_cost_per_contract) if structure.kind.value == "iron_condor" else 0
    exit_plan = build_exit_plan(structure.kind, settings.risk, credit_per_share=credit, debit_per_share=debit, width_per_share=width)
    thesis_payload = {
        "position_id": f"{event.event_id}:{structure.structure_id[:8]}",
        "expected_move_pct": metrics.expected_move_pct,
        "expected_hold_days": max(1, (structure.expires_on - today).days // 2),
        "invalidation": f"spot beyond {[round(x, 1) for x in structure.breakevens]} OR vrp<0 OR dte<=1",
        "expected_pnl_pct": 0.4 if structure.kind.value.endswith("vertical") else 0.55,
        "operator_waiver": sorted(c.value for c in failed_codes),
        "operator_reason": reason,
    }
    thesis_hash = stable_hash(thesis_payload)
    position_id = f"{event.event_id}:{structure.structure_id[:8]}"
    cid = client_order_id(structure, event.event_id, today.isoformat())
    limit = signed_limit(structure, abs(structure.entry_cost_per_contract))
    pos = Position(position_id=position_id, event_id=event.event_id, symbol=event.symbol,
                   structure=structure, entry_order_client_id=cid,
                   status=PositionStatus.PENDING_FILL, exit_plan=exit_plan, opened_at=now)
    state.upsert_position(pos)
    try:
        import json as _json
        state.put_thesis(position_id, event.event_id, event.symbol, _json.dumps(thesis_payload), thesis_hash)
        journal.append("THESIS", {"position_id": position_id, "thesis": thesis_payload, "hash": thesis_hash}, cycle_id)
    except Exception:
        pass
    journal.append("OPERATOR_OVERRIDE", {
        "symbol": symbol, "structure": structure.label, "refused": False,
        "waived": sorted(c.value for c in failed_codes),
        "safety_gates": "G1-G8 all passed",
        "conviction": conviction, "contracts": qty,
        "max_loss_usd": per_contract * qty,
        "limit": limit, "client_order_id": cid,
        "operator_reason": reason,
    }, cycle_id)

    order = submit_entry(structure, event.event_id, today.isoformat(), qty, limit,
                         cycle_id, journal, state, broker, position_id)
    if order is None:
        pos.status = PositionStatus.REJECTED
        state.upsert_position(pos)
        return {"refused": "broker submit failed (see ORDER_REJECTED)", "client_order_id": cid}
    pos.broker_order_id = str(order.get("id", "")) if isinstance(order, dict) else None
    state.upsert_position(pos)
    state.incr_entries(today)
    return {"submitted": True, "position_id": position_id, "client_order_id": cid,
            "broker_order_id": pos.broker_order_id, "contracts": qty,
            "max_loss_usd": per_contract * qty, "limit": limit}
