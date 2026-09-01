"""Black-Scholes pricing utilities — used for independent verification.

We deliberately re-derive IV and delta from observed option mids rather than
trusting any model output from a data vendor: the numbers that drive gates
must be reproducible from raw inputs (P3)."""

from __future__ import annotations

from math import erf, exp, log, sqrt

RISK_FREE = 0.04  # fixed assumption, documented in ARCHITECTURE.md


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + erf(x / sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    return exp(-x * x / 2.0) / sqrt(2.0 * 3.141592653589793)


def _d1(S: float, K: float, T: float, r: float, sigma: float) -> float:
    return (log(S / K) + (r + sigma * sigma / 2.0) * T) / (sigma * sqrt(T))


def bs_price(S: float, K: float, T: float, sigma: float, is_call: bool,
             r: float = RISK_FREE) -> float:
    if T <= 0 or sigma <= 0:
        intrinsic = max(S - K, 0.0) if is_call else max(K - S, 0.0)
        return intrinsic
    d1 = _d1(S, K, T, r, sigma)
    d2 = d1 - sigma * sqrt(T)
    if is_call:
        return S * _norm_cdf(d1) - K * exp(-r * T) * _norm_cdf(d2)
    return K * exp(-r * T) * _norm_cdf(-d2) - S * _norm_cdf(-d1)


def bs_delta(S: float, K: float, T: float, sigma: float, is_call: bool,
             r: float = RISK_FREE) -> float:
    if T <= 0 or sigma <= 0:
        return 0.0
    d1 = _d1(S, K, T, r, sigma)
    delta = _norm_cdf(d1)
    return delta if is_call else delta - 1.0


def implied_vol(price: float, S: float, K: float, T: float, is_call: bool,
                r: float = RISK_FREE) -> float | None:
    """Bisection IV solver. Returns None when the price is outside no-arb
    bounds — a None IV must fail gates closed, never be defaulted."""
    if price <= 0 or S <= 0 or K <= 0 or T <= 0:
        return None
    intrinsic = max(S - K, 0.0) if is_call else max(K - S, 0.0)
    if price < intrinsic * 0.98:
        return None
    lo, hi = 1e-4, 5.0
    f_lo = bs_price(S, K, T, lo, is_call, r) - price
    f_hi = bs_price(S, K, T, hi, is_call, r) - price
    if f_lo > 0 or f_hi < 0:
        return None  # price not bracketable -> not trustworthy
    for _ in range(80):
        mid = (lo + hi) / 2
        f_mid = bs_price(S, K, T, mid, is_call, r) - price
        if abs(f_mid) < 1e-6:
            return mid
        if f_mid > 0:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


def straddle_iv(call_mid: float, put_mid: float, S: float, K: float, T: float,
                r: float = RISK_FREE) -> float | None:
    """Implied vol that reprices the ATM straddle at observed mid."""
    if T <= 0 or S <= 0 or K <= 0:
        return None
    target = call_mid + put_mid
    if target <= 0:
        return None

    def f(sigma: float) -> float:
        return bs_price(S, K, T, sigma, True, r) + bs_price(S, K, T, sigma, False, r) - target

    lo, hi = 1e-4, 5.0
    if f(lo) > 0 or f(hi) < 0:
        return None
    for _ in range(80):
        mid = (lo + hi) / 2
        if abs(f(mid)) < 1e-6:
            return mid
        if f(mid) > 0:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2
