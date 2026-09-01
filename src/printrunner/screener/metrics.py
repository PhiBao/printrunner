"""Screener metrics — pure deterministic functions from a validated snapshot."""

from __future__ import annotations

import math
from datetime import date

from ..config import RiskParams
from ..domain import EarningsEvent, MarketDataSnapshot, Metrics, GateCode
from ..marketdata.bs import straddle_iv


def select_expiry(expiries: list[date], event_date: date) -> date | None:
    """First expiry in [event+4, event+10] closest to event+7."""
    cands = [e for e in expiries if 4 <= (e - event_date).days <= 10]
    if not cands:
        return None
    target = event_date.toordinal() + 7
    return min(cands, key=lambda e: abs(e.toordinal() - target))


def _annualized_hv(closes: list[float], window: int = 20) -> float | None:
    if len(closes) < window + 1:
        return None
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    recent = rets[-window:]
    mean = sum(recent) / len(recent)
    var = sum((r - mean) ** 2 for r in recent) / (len(recent) - 1) if len(recent) > 1 else 0.0
    return math.sqrt(var * 252) if var > 0 else 0.0


def _atm_pair(chain, spot: float, expiry: date):
    """Find ATM call+put for an expiry. Returns (call, put) or (None, None)."""
    legs = [q for q in chain if q.expiry == expiry]
    if not legs:
        return None, None
    strikes = sorted({q.strike for q in legs})
    atm_strike = min(strikes, key=lambda k: abs(k - spot))
    call = next((q for q in legs if q.strike == atm_strike and q.option_type == "call" and q.mid > 0), None)
    put = next((q for q in legs if q.strike == atm_strike and q.option_type == "put" and q.mid > 0), None)
    return call, put


def compute_metrics(
    snapshot: MarketDataSnapshot,
    event: EarningsEvent,
    today: date,
    risk: RiskParams,
) -> tuple[Metrics | None, list[tuple[GateCode, str]], date | None]:
    """Compute deterministic metrics. On missing data returns (None, failures)."""
    failures: list[tuple[GateCode, str]] = []

    if not snapshot.chain or snapshot.spot <= 0:
        failures.append((GateCode.G1, "empty chain or bad spot"))
        return None, failures, None

    expiries = sorted({q.expiry for q in snapshot.chain})
    expiry = select_expiry(expiries, event.event_date)
    if expiry is None:
        failures.append((GateCode.G5, "no expiry in [event+4, event+10]"))
        return None, failures, None

    call, put = _atm_pair(snapshot.chain, snapshot.spot, expiry)
    if call is None or put is None:
        failures.append((GateCode.G1, "no ATM straddle quotes for chosen expiry"))
        return None, failures, expiry

    em_usd = call.mid + put.mid
    em_pct = em_usd / snapshot.spot if snapshot.spot else 0

    # hist move sample
    hist = snapshot.hist_earn_moves
    if len(hist) < risk.em_hist_min:
        failures.append((GateCode.G1, f"insufficient earnings history ({len(hist)} < {risk.em_hist_min})"))
        return None, failures, expiry
    avg_hist = sum(hist) / len(hist) if hist else 0
    move_ratio = em_pct / avg_hist if avg_hist else 0

    # runup drift
    closes = snapshot.closes_recent
    if len(closes) < risk.runup_lookback_days + 1:
        failures.append((GateCode.G1, "insufficient closes for runup drift"))
        return None, failures, expiry
    runup_drift = closes[-1] / closes[-(risk.runup_lookback_days + 1)] - 1

    # VRP: straddle IV - hv20
    vrp: float | None = None
    hv = snapshot.hv20
    if hv is None and len(closes) >= 21:
        hv = _annualized_hv(closes, 20)
    T = max(1, (expiry - today).days) / 365.0
    # use ATM strike for IV solve
    atm_strike = call.strike
    iv = straddle_iv(call.mid, put.mid, snapshot.spot, atm_strike, T)
    if iv is not None and hv is not None:
        vrp = iv - hv

    metrics = Metrics(
        expected_move_pct=em_pct,
        expected_move_usd=em_usd,
        move_ratio=move_ratio,
        hist_move_sample=len(hist),
        runup_drift=runup_drift,
        vrp=vrp,
        iv_rank=snapshot.iv_rank,
        spy_ret_1d=snapshot.spy_ret_1d,
        spy_ret_5d=snapshot.spy_ret_5d,
    )
    return metrics, failures, expiry
