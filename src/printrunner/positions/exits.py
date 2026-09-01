"""Deterministic exit plans and exit evaluation.

Exit rules are computed once at entry (from code, never from the LLM) and
evaluated against fresh quotes. "Cannot value" is a close trigger (P3).
"""

from __future__ import annotations

from datetime import date

from ..config import RiskParams
from ..domain import ExitPlan, OptionQuote, Position, StructureKind


def build_exit_plan(structure: StructureKind, risk: RiskParams, credit_per_share: float = 0.0,
                    debit_per_share: float = 0.0, width_per_share: float = 0.0) -> ExitPlan:
    if structure == StructureKind.IRON_CONDOR:
        mtm = min(risk.condor_mtm_loss_credit_mult * credit_per_share,
                  risk.condor_mtm_loss_maxloss_pct * (width_per_share - credit_per_share))
        return ExitPlan(
            profit_target_pct=risk.condor_profit_capture_pct,
            stop_loss_pct=0.0,
            credit_capture_pct=risk.condor_profit_capture_pct,
            condor_mtm_stop=mtm,
        )
    return ExitPlan(
        profit_target_pct=risk.vertical_profit_target_pct,
        stop_loss_pct=risk.vertical_stop_loss_pct,
    )


def _vertical_value(legs: list, quotes_by_symbol: dict[str, OptionQuote]) -> float | None:
    """Current mid value of a vertical spread from fresh quotes: long - short."""
    vals = {}
    for leg in legs:
        q = quotes_by_symbol.get(leg.option_symbol)
        if q is None or q.mid <= 0:
            return None
        vals[leg.option_symbol] = q.mid
    # convention: legs[0]=long, legs[1]=short for verticals
    return vals[legs[0].option_symbol] - vals[legs[1].option_symbol]


def _condor_cost(legs: list, quotes: dict[str, OptionQuote]) -> float | None:
    """Current cost to buy back a condor: shorts - longs."""
    vals = {}
    for leg in legs:
        q = quotes.get(leg.option_symbol)
        if q is None or q.mid <= 0:
            return None
        vals[leg.option_symbol] = q.mid
    # legs: [LP buy, SP sell, SC sell, LC buy]
    long_put, short_put, short_call, long_call = [leg.option_symbol for leg in legs]
    return (vals[short_put] + vals[short_call]) - (vals[long_put] + vals[long_call])


def evaluate_exit(
    position: Position,
    quotes: dict[str, OptionQuote],
    spot: float,
    today: date,
    risk: RiskParams,
) -> tuple[str, str]:
    """Returns (action, reason). action in {HOLD, CLOSE}."""
    # DTE guard
    dte = (position.structure.expires_on - today).days
    if dte <= 1:
        return "CLOSE", "dte_le_1"

    kind = position.structure.kind
    entry = abs(position.structure.entry_cost_per_contract)  # per-share magnitude

    if kind in (StructureKind.CALL_DEBIT_VERTICAL, StructureKind.PUT_DEBIT_VERTICAL):
        val = _vertical_value(position.structure.legs, quotes)
        if val is None:
            return "CLOSE", "cannot_value_vertical"
        pnl_pct = (val - entry) / entry if entry else 0
        if pnl_pct >= position.exit_plan.profit_target_pct:
            return "CLOSE", f"target {pnl_pct:.1%} >= {position.exit_plan.profit_target_pct:.0%}"
        if pnl_pct <= position.exit_plan.stop_loss_pct:
            return "CLOSE", f"stop {pnl_pct:.1%} <= {position.exit_plan.stop_loss_pct:.0%}"
        if dte < risk.min_expiry_days_after_exit and pnl_pct < 0.20:
            return "CLOSE", f"dte {dte} and pnl {pnl_pct:.1%} < 20%"
        return "HOLD", f"vertical pnl {pnl_pct:.1%} dte {dte}"

    # iron condor
    cost = _condor_cost(position.structure.legs, quotes)
    if cost is None:
        return "CLOSE", "cannot_value_condor"
    credit = abs(position.structure.entry_cost_per_contract)
    profit_frac = (credit - cost) / credit if credit else 0
    if profit_frac >= (position.exit_plan.credit_capture_pct or 0.55):
        return "CLOSE", f"condor profit capture {profit_frac:.1%}"
    mtm_loss = cost - credit
    if position.exit_plan.condor_mtm_stop is not None and mtm_loss >= position.exit_plan.condor_mtm_stop:
        return "CLOSE", f"condor mtm stop {mtm_loss:.2f} >= {position.exit_plan.condor_mtm_stop:.2f}"
    if dte <= 2:
        return "CLOSE", f"condor dte {dte} <= 2"
    # breach: spot within 0.75*wing of either short strike (warning only), beyond short+wing => close
    # short put = legs[1], short call = legs[2]
    try:
        short_put_q = quotes[position.structure.legs[1].option_symbol]
        short_call_q = quotes[position.structure.legs[2].option_symbol]
        wing = max(
            short_put_q.strike - quotes[position.structure.legs[0].option_symbol].strike,
            quotes[position.structure.legs[3].option_symbol].strike - short_call_q.strike,
        )
        if spot <= short_put_q.strike - wing or spot >= short_call_q.strike + wing:
            return "CLOSE", "spot breached wing"
    except Exception:
        pass
    return "HOLD", f"condor pnl {profit_frac:.1%} dte {dte}"
