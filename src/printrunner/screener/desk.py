"""Structure desk — builds defined-risk candidates from real quotes.

Every price and loss figure comes from observed mids; no model assert.
Up to three structures per event: call-debit vertical, put-debit vertical,
and one iron condor. Only structures that pass internal sanity are returned;
the LLM merely picks among them (P1)."""

from __future__ import annotations

from datetime import date

from ..config import RiskParams
from ..domain import (
    EarningsEvent,
    Leg,
    MarketDataSnapshot,
    Metrics,
    OptionQuote,
    Structure,
    StructureKind,
    stable_hash,
)
from ..marketdata.bs import bs_delta, straddle_iv


def _delta_for_quote(q: OptionQuote, spot: float, today: date, iv: float | None) -> float | None:
    T = max(1, (q.expiry - today).days) / 365.0
    sigma = q.implied_vol if q.implied_vol is not None else iv
    if sigma is None or sigma <= 0:
        return None
    is_call = q.option_type == "call"
    return bs_delta(spot, q.strike, T, sigma, is_call)


def _choose_vertical_legs(
    chain: list[OptionQuote],
    spot: float,
    expiry: date,
    today: date,
    is_call: bool,
    risk: RiskParams,
    atm_iv: float | None,
) -> tuple[OptionQuote, OptionQuote] | None:
    """Pick long (delta ~0.50) and short (delta ~0.25) legs for a debit vertical."""
    legs = [q for q in chain if q.expiry == expiry and q.option_type == ("call" if is_call else "put")
            and q.mid > 0 and q.bid > 0 and q.ask >= q.bid]
    if len(legs) < 2:
        return None
    scored: list[tuple[OptionQuote, float]] = []
    for q in legs:
        d = _delta_for_quote(q, spot, today, atm_iv)
        if d is None:
            continue
        scored.append((q, d))
    if not scored:
        return None
    if is_call:
        longs = [(q, d) for q, d in scored if 0.45 <= d <= 0.60]
        shorts = [(q, d) for q, d in scored if 0.20 <= d <= 0.32]
        if not longs or not shorts:
            return None
        long = min(longs, key=lambda x: abs(x[1] - 0.50))[0]
        # short must be strictly OTM beyond long
        shorts = [(q, d) for q, d in shorts if q.strike > long.strike]
        if not shorts:
            return None
        short = min(shorts, key=lambda x: abs(x[1] - 0.25))[0]
    else:
        # puts: delta is negative, compare abs or raw
        longs = [(q, d) for q, d in scored if -0.60 <= d <= -0.45]
        shorts = [(q, d) for q, d in scored if -0.32 <= d <= -0.20]
        if not longs or not shorts:
            return None
        long = min(longs, key=lambda x: abs(x[1] + 0.50))[0]
        shorts = [(q, d) for q, d in shorts if q.strike < long.strike]
        if not shorts:
            return None
        short = min(shorts, key=lambda x: abs(x[1] + 0.25))[0]
    return long, short


def _structure_id(kind: StructureKind, legs: list[Leg], event: EarningsEvent) -> str:
    payload = {"event": event.event_id, "kind": kind.value, "legs": [l.option_symbol for l in legs]}
    return stable_hash(payload)


def build_structures(
    snapshot: MarketDataSnapshot,
    metrics: Metrics,
    event: EarningsEvent,
    today: date,
    risk: RiskParams,
) -> list[Structure]:
    """Return 0-3 structures that are structurally sound. Sizing stays at 1
    contract; the gate engine applies conviction sizing."""
    chain = snapshot.chain
    if not chain:
        return []
    expiry = chain[0].expiry  # snapshot builder loaded one expiry; treat as chosen
    # fallback: if chain contains multiple expiries, pick the one in metrics window
    expiries = sorted({q.expiry for q in chain})
    if len(expiries) > 1:
        # prefer expiry closest to event+7 within [4,10]
        from .metrics import select_expiry

        chosen = select_expiry(expiries, event.event_date)
        if chosen:
            expiry = chosen

    # ATM iv for delta estimation
    iv: float | None = None
    try:
        # find ATM straddle for iv
        strikes = sorted({q.strike for q in chain if q.expiry == expiry})
        atm_strike = min(strikes, key=lambda k: abs(k - snapshot.spot))
        call_atm = next((q for q in chain if q.expiry == expiry and q.strike == atm_strike and q.option_type == "call"), None)
        put_atm = next((q for q in chain if q.expiry == expiry and q.strike == atm_strike and q.option_type == "put"), None)
        if call_atm and put_atm and call_atm.mid > 0 and put_atm.mid > 0:
            T = max(1, (expiry - today).days) / 365.0
            iv = straddle_iv(call_atm.mid, put_atm.mid, snapshot.spot, atm_strike, T)
    except Exception:
        iv = None

    out: list[Structure] = []

    # --- call debit vertical
    pick = _choose_vertical_legs(chain, snapshot.spot, expiry, today, True, risk, iv)
    if pick:
        long, short = pick
        debit = long.mid - short.mid
        width = short.strike - long.strike
        if debit > 0 and debit < width and debit * 100 <= risk.vertical_max_debit:
            max_loss = debit * 100
            max_profit = (width - debit) * 100
            breakeven = long.strike + debit
            sid = _structure_id(StructureKind.CALL_DEBIT_VERTICAL, [], event)
            legs = [
                Leg(option_symbol=long.option_symbol, side="buy", ratio=1, quote_at_selection=long),
                Leg(option_symbol=short.option_symbol, side="sell", ratio=1, quote_at_selection=short),
            ]
            sid = _structure_id(StructureKind.CALL_DEBIT_VERTICAL, legs, event)
            out.append(Structure(
                structure_id=sid,
                kind=StructureKind.CALL_DEBIT_VERTICAL,
                legs=legs,
                expires_on=expiry,
                max_loss_per_contract=max_loss,
                max_profit_per_contract=max_profit,
                breakevens=[breakeven],
                entry_cost_per_contract=debit,
                label=f"{event.symbol} {long.strike:.0f}/{short.strike:.0f}C {expiry.isoformat()} debit {debit:.2f}",
            ))

    # --- put debit vertical
    pick = _choose_vertical_legs(chain, snapshot.spot, expiry, today, False, risk, iv)
    if pick:
        long, short = pick
        debit = long.mid - short.mid
        width = long.strike - short.strike
        if debit > 0 and debit < width and debit * 100 <= risk.vertical_max_debit:
            max_loss = debit * 100
            max_profit = (width - debit) * 100
            breakeven = long.strike - debit
            legs = [
                Leg(option_symbol=long.option_symbol, side="buy", ratio=1, quote_at_selection=long),
                Leg(option_symbol=short.option_symbol, side="sell", ratio=1, quote_at_selection=short),
            ]
            sid = _structure_id(StructureKind.PUT_DEBIT_VERTICAL, legs, event)
            out.append(Structure(
                structure_id=sid,
                kind=StructureKind.PUT_DEBIT_VERTICAL,
                legs=legs,
                expires_on=expiry,
                max_loss_per_contract=max_loss,
                max_profit_per_contract=max_profit,
                breakevens=[breakeven],
                entry_cost_per_contract=debit,
                label=f"{event.symbol} {short.strike:.0f}/{long.strike:.0f}P {expiry.isoformat()} debit {debit:.2f}",
            ))

    # --- iron condor (needs sufficient strikes)
    condor = _build_condor(chain, snapshot.spot, expiry, metrics, event, risk, today)
    if condor:
        out.append(condor)

    return out[:3]


def _build_condor(
    chain: list[OptionQuote],
    spot: float,
    expiry: date,
    metrics: Metrics,
    event: EarningsEvent,
    risk: RiskParams,
    today: date,
) -> Structure | None:
    # only offered when VRP and move ratio signal premium richness — the gate
    # re-validates, but we save the model from an obviously wrong choice.
    if metrics.vrp is not None and metrics.vrp <= 0:
        return None
    if metrics.move_ratio < risk.move_ratio_crush_min:
        return None

    em_usd = metrics.expected_move_usd
    if em_usd <= 0:
        return None
    short_call_target = spot + risk.condor_short_em_multiple * em_usd
    short_put_target = spot - risk.condor_short_em_multiple * em_usd

    calls = sorted([q for q in chain if q.expiry == expiry and q.option_type == "call" and q.mid > 0],
                   key=lambda q: q.strike)
    puts = sorted([q for q in chain if q.expiry == expiry and q.option_type == "put" and q.mid > 0],
                  key=lambda q: q.strike)

    def nearest(cands: list[OptionQuote], target: float, above: bool) -> OptionQuote | None:
        pool = [q for q in cands if (q.strike >= target) == above]
        if not pool:
            return None
        return min(pool, key=lambda q: abs(q.strike - target))

    sc = nearest(calls, short_call_target, True)
    sp = nearest(puts, short_put_target, False)
    if sc is None or sp is None:
        return None

    # wings: next listed strikes beyond shorts (grid step = min diff nearby)
    all_call_strikes = [q.strike for q in calls]
    all_put_strikes = [q.strike for q in puts]
    try:
        sc_idx = all_call_strikes.index(sc.strike)
        sp_idx = all_put_strikes.index(sp.strike)
    except ValueError:
        return None
    # wing distance: try 2 grid steps beyond short
    wing_steps = 2
    lc = None
    for step in range(wing_steps, wing_steps + 3):
        candidates = [q for q in calls if all_call_strikes.index(q.strike) == sc_idx + step] if sc_idx + step < len(calls) else []
        if candidates:
            lc = candidates[0]
            break
    lp = None
    for step in range(wing_steps, wing_steps + 3):
        candidates = [q for q in puts if all_put_strikes.index(q.strike) == sp_idx - step] if sp_idx - step >= 0 else []
        if candidates:
            lp = candidates[0]
            break
    if lc is None or lp is None:
        return None

    credit = (sc.mid + sp.mid) - (lc.mid + lp.mid)
    call_width = lc.strike - sc.strike
    put_width = sp.strike - lp.strike
    width = max(call_width, put_width)
    min_credit = max(risk.condor_min_credit, risk.condor_min_credit_width_pct * width)
    if credit < min_credit or width <= 0:
        return None
    max_loss = (width - credit) * 100
    max_profit = credit * 100
    if max_loss <= 0:
        return None

    legs = [
        Leg(option_symbol=lp.option_symbol, side="buy", ratio=1, quote_at_selection=lp),
        Leg(option_symbol=sp.option_symbol, side="sell", ratio=1, quote_at_selection=sp),
        Leg(option_symbol=sc.option_symbol, side="sell", ratio=1, quote_at_selection=sc),
        Leg(option_symbol=lc.option_symbol, side="buy", ratio=1, quote_at_selection=lc),
    ]
    sid = _structure_id(StructureKind.IRON_CONDOR, legs, event)
    return Structure(
        structure_id=sid,
        kind=StructureKind.IRON_CONDOR,
        legs=legs,
        expires_on=expiry,
        max_loss_per_contract=max_loss,
        max_profit_per_contract=max_profit,
        breakevens=[sp.strike - credit, sc.strike + credit],
        entry_cost_per_contract=-credit,  # credit received -> negative cost
        label=f"{event.symbol} {lp.strike:.0f}/{sp.strike:.0f}P/{sc.strike:.0f}/{lc.strike:.0f} {expiry.isoformat()} credit {credit:.2f}",
    )
