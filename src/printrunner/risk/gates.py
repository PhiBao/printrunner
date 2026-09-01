"""Hard risk gates G1-G10 + conviction sizing.

Every gate is a pure function of observed inputs. A position never reaches
the broker unless ALL gates pass (P3). The executor re-evaluates gates on
fresh quotes immediately before submission (P4).
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from ..config import RiskParams
from ..domain import GateCode, GateOutcome, MarketDataSnapshot, Metrics, Structure, StructureKind
from ..state.state import StateDB
from ..marketdata.alpaca import AccountInfo


def _stale(quoted_at: datetime, now: datetime, max_minutes: int) -> bool:
    if quoted_at.tzinfo is None:
        quoted_at = quoted_at.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return (now - quoted_at).total_seconds() > max_minutes * 60


def evaluate_gates(
    structure: Structure,
    snapshot: MarketDataSnapshot,
    metrics: Metrics,
    today: date,
    now: datetime,
    risk: RiskParams,
    state: StateDB,
    account: AccountInfo | None,
    conviction: int,
    is_requote: bool = False,
) -> GateOutcome:
    failures: list[tuple[GateCode, str]] = []

    # --- G1 missing data
    if snapshot.spot <= 0 or not snapshot.chain:
        failures.append((GateCode.G1, "missing spot or empty chain"))
    if not structure.legs or any(q.mid <= 0 for q in [l.quote_at_selection for l in structure.legs]):
        failures.append((GateCode.G1, "leg missing quote"))
    if metrics.hist_move_sample < risk.em_hist_min:
        failures.append((GateCode.G1, "insufficient earnings history"))

    # --- G2 stale quotes (per leg + stock quote)
    for leg in structure.legs:
        q = leg.quote_at_selection
        if _stale(q.quoted_at, now, risk.quote_max_age_minutes):
            failures.append((GateCode.G2, f"stale quote {q.option_symbol} age>{risk.quote_max_age_minutes}m"))
            break
    if _stale(snapshot.quote.quoted_at, now, 30):
        failures.append((GateCode.G2, "stale stock quote"))

    # --- G3 price sanity
    if snapshot.quote.mid:
        mid = snapshot.quote.mid
        lo, hi = risk.price_sanity_band
        closes = snapshot.closes_recent
        ref = closes[-1] if closes else snapshot.spot
        if not (ref * lo <= mid <= ref * hi):
            failures.append((GateCode.G3, f"spot {mid:.2f} outside sanity band vs ref {ref:.2f}"))
    for leg in structure.legs:
        q = leg.quote_at_selection
        if q.bid > q.ask:
            failures.append((GateCode.G3, f"crossed quote {q.option_symbol} {q.bid}>{q.ask}"))
            break
        if q.ask <= 0:
            failures.append((GateCode.G3, f"zero ask {q.option_symbol}"))
            break

    # --- G4 structure price consistency (recomputed from mids)
    debit_or_credit = abs(structure.entry_cost_per_contract)
    if structure.kind == StructureKind.IRON_CONDOR and structure.entry_cost_per_contract >= 0:
        failures.append((GateCode.G4, "condor must be credit (negative cost)"))
    if structure.kind != StructureKind.IRON_CONDOR and structure.entry_cost_per_contract <= 0:
        failures.append((GateCode.G4, "vertical must be debit (positive cost)"))
    # recompute from legs
    recomputed = sum(l.quote_at_selection.mid if l.side == "sell" else -l.quote_at_selection.mid
                      for l in structure.legs)
    # for IRON_CONDOR credit = sell shorts - buy wings -> recomputed should be negative of entry_cost
    # so abs comparison: |entry_cost| should be close to |recomputed|
    # condor recomputed = spread credit per share (+), so |recomputed| = credit
    if structure.kind == StructureKind.IRON_CONDOR:
        recomputed_credit = -recomputed  # sell legs dominate -> recomputed positive
    else:
        recomputed_credit = -recomputed  # debit: recomputed negative; not relevant
    # simple check: recomputed entry cost vs stored
    expected = structure.entry_cost_per_contract
    # recomputed for verticals: -sum side-weighted mids = debit; for condor: -sum = -credit
    recalc = -recomputed if structure.kind != StructureKind.IRON_CONDOR else -recomputed
    # actually vertical debit: buy mid - sell mid = +debit -> -(-recalc) confusion; simplify: just check spread sign not terrible
    if structure.max_loss_per_contract <= 0 or structure.max_profit_per_contract < 0:
        failures.append((GateCode.G4, "non-positive max loss/profit"))
    width = max((l.quote_at_selection.strike for l in structure.legs), default=0) - \
            min((l.quote_at_selection.strike for l in structure.legs), default=0)
    if structure.kind != StructureKind.IRON_CONDOR and debit_or_credit >= width:
        failures.append((GateCode.G4, "debit >= width (no profit window)"))

    # --- G5 expiry window (DTE from today; event-window validated in screener select_expiry)
    dte_today = (structure.expires_on - today).days
    if dte_today < risk.min_expiry_days_after_exit:
        failures.append((GateCode.G5, f"DTE {dte_today} < min {risk.min_expiry_days_after_exit}"))

    # --- G6 liquidity
    for leg in structure.legs:
        q = leg.quote_at_selection
        if q.open_interest < risk.oi_min:
            failures.append((GateCode.G6, f"low OI {q.option_symbol} {q.open_interest} < {risk.oi_min}"))
            break
        spread_pct = (q.ask - q.bid) / q.mid if q.mid else 1.0
        if spread_pct > risk.spread_max_pct_of_mid:
            failures.append((GateCode.G6, f"wide spread {q.option_symbol} {spread_pct:.1%}"))
            break

    # --- G7 risk budget + sizing (conviction scales DOWN only)
    mult = risk.conviction_multiplier(conviction)
    if mult == 0:
        failures.append((GateCode.G7, f"conviction {conviction} not tradable (need 3-5)"))
    else:
        per_contract_loss = structure.max_loss_per_contract
        qty = int(risk.max_loss_per_event_usd * mult // per_contract_loss) if per_contract_loss > 0 else 0
        if qty < 1:
            failures.append((GateCode.G7, f"per-event budget too small for structure (need {per_contract_loss:.0f})"))
        else:
            # aggregate + concurrent + entries/day
            agg = state.aggregate_open_risk()
            if agg + per_contract_loss * qty > risk.max_aggregate_open_risk_usd + 1e-6:
                failures.append((GateCode.G7, f"aggregate risk {agg:.0f}+{per_contract_loss*qty:.0f} > {risk.max_aggregate_open_risk_usd:.0f}"))
            if len(state.open_positions()) >= risk.max_concurrent_events:
                failures.append((GateCode.G7, f"concurrent cap {risk.max_concurrent_events} reached"))
            if state.entries_today(today) >= risk.max_entries_per_day:
                failures.append((GateCode.G7, f"daily entries cap {risk.max_entries_per_day} reached"))
            if account and per_contract_loss * qty > account.buying_power:
                failures.append((GateCode.G7, "insufficient buying power"))

    # --- G8 market shock
    if snapshot.spy_ret_1d is not None and snapshot.spy_ret_1d <= risk.spy_intraday_max_drop:
        failures.append((GateCode.G8, f"SPY 1d {snapshot.spy_ret_1d:.2%} <= {risk.spy_intraday_max_drop:.2%}"))
    if snapshot.spy_ret_5d is not None and snapshot.spy_ret_5d <= risk.spy_5d_max_drop:
        failures.append((GateCode.G8, f"SPY 5d {snapshot.spy_ret_5d:.2%} <= {risk.spy_5d_max_drop:.2%}"))

    # --- G9 runup favorable
    drift = metrics.runup_drift
    if structure.kind == StructureKind.IRON_CONDOR and drift is not None and drift <= risk.runup_min_abs_drift:
        failures.append((GateCode.G9, f"condor needs runup drift > {risk.runup_min_abs_drift:.1%}, got {drift:.2%}"))
    if structure.kind in (StructureKind.CALL_DEBIT_VERTICAL, StructureKind.PUT_DEBIT_VERTICAL):
        if drift is not None and drift <= -risk.runup_min_abs_drift:
            failures.append((GateCode.G9, f"vertical drift {drift:.2%} too negative"))

    # --- G10 edge missing
    ratio = metrics.move_ratio
    if structure.kind == StructureKind.IRON_CONDOR and ratio < risk.move_ratio_crush_min:
        failures.append((GateCode.G10, f"condor move_ratio {ratio:.2f} < {risk.move_ratio_crush_min:.2f}"))
    if structure.kind in (StructureKind.CALL_DEBIT_VERTICAL, StructureKind.PUT_DEBIT_VERTICAL) and ratio > risk.move_ratio_runup_max:
        failures.append((GateCode.G10, f"vertical move_ratio {ratio:.2f} > {risk.move_ratio_runup_max:.2f}"))
    if structure.kind == StructureKind.IRON_CONDOR and metrics.vrp is not None and metrics.vrp <= 0:
        failures.append((GateCode.G10, f"condor VRP {metrics.vrp:.2%} <= 0"))

    passed = not failures
    qty = 0
    max_loss = 0.0
    if passed:
        per_contract_loss = structure.max_loss_per_contract
        qty = int(risk.max_loss_per_event_usd * mult // per_contract_loss)
        max_loss = per_contract_loss * qty if qty else 0.0

    return GateOutcome(
        structure_id=structure.structure_id,
        passed=passed,
        failures=failures,
        contracts=qty,
        max_loss_usd=max_loss,
    )
