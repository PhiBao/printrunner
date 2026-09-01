"""Alpaca market-data service: rate-limited and cached.

Every raw number used by gates flows through here. Caching is time-boxed so
stale data can't silently drive decisions: daily bars cache keyed to the
trading day, option chains 10 minutes, option snapshots 60 seconds.
`force=True` bypasses the snapshot cache — the executor uses that to re-quote
legs immediately before order submission (P4).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from ..config import Settings
from ..domain import OptionQuote, StockQuote, utcnow

MIN_CALL_INTERVAL = 0.35  # seconds; keeps us comfortably under rate limits
BARS_TTL_DAYS = 1
CHAIN_TTL = 600.0
SNAP_TTL = 60.0


class MarketDataError(RuntimeError):
    pass


@dataclass
class ClockInfo:
    is_open: bool
    next_open: datetime
    next_close: datetime


@dataclass
class AccountInfo:
    equity: float
    buying_power: float
    cash: float


@dataclass
class Bar:
    trade_date: date
    open: float
    high: float
    low: float
    close: float
    volume: int


@dataclass
class Contract:
    symbol: str  # OCC symbol from Alpaca — we never construct OCC ourselves
    strike: float
    expiry: date
    option_type: str  # "call" | "put"


def _mapping(response, symbols: list[str]) -> dict:
    """Normalize alpaca-py response shapes (dict-like or attribute-carrying)."""
    if isinstance(response, dict):
        return response
    for attr in ("snapshots", "quotes", "trades", "bars"):
        val = getattr(response, attr, None)
        if isinstance(val, dict):
            return val
    out = {}
    for s in symbols:
        try:
            out[s] = response[s]  # type: ignore[index]
        except Exception:
            continue
    return out


def _bars_list(response, symbol: str) -> list:
    if isinstance(response, dict):
        return response.get(symbol, [])
    if isinstance(response, (list, tuple)):
        return list(response)
    bars = getattr(response, "bars", None)
    if isinstance(bars, dict):
        return bars.get(symbol, [])
    if bars is not None:
        return list(bars)
    try:
        return list(response[symbol])  # type: ignore[index]
    except Exception:
        return []


class AlpacaMarketData:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        key, secret = settings.alpaca_key_id, settings.alpaca_secret
        if not key or not secret:
            raise MarketDataError("alpaca credentials missing (ALPACA_API_KEY_ID/ALPACA_SECRET_KEY)")
        try:
            from alpaca.trading.client import TradingClient
            from alpaca.data.historical import (
                OptionHistoricalDataClient,
                StockHistoricalDataClient,
            )
        except Exception as exc:  # pragma: no cover
            raise MarketDataError(f"alpaca-py import failed: {exc}") from exc
        self._trading = TradingClient(key, secret, url_override=settings.alpaca_base_url)
        self._stocks = StockHistoricalDataClient(key, secret)
        self._options = OptionHistoricalDataClient(key, secret)
        self._last_call = 0.0
        self._bars_cache: dict[str, tuple[date, list[Bar]]] = {}
        self._chain_cache: dict[tuple[str, date, date], tuple[float, list[Contract]]] = {}
        self._snap_cache: dict[str, tuple[float, OptionQuote]] = {}
        self._deltas: dict[str, float] = {}

    # ------------------------------------------------------------- plumbing
    def _throttle(self) -> None:
        now = time.monotonic()
        wait = MIN_CALL_INTERVAL - (now - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()

    # ----------------------------------------------------------------- clock
    def clock(self) -> ClockInfo:
        self._throttle()
        try:
            c = self._trading.get_clock()
            return ClockInfo(is_open=bool(c.is_open), next_open=c.next_open, next_close=c.next_close)
        except Exception as exc:
            raise MarketDataError(f"clock fetch failed: {exc}") from exc

    def account(self) -> AccountInfo:
        self._throttle()
        try:
            acct = self._trading.get_account()
            return AccountInfo(
                equity=float(acct.equity),
                buying_power=float(acct.buying_power or 0),
                cash=float(acct.cash or 0),
            )
        except Exception as exc:
            raise MarketDataError(f"account fetch failed: {exc}") from exc

    # ------------------------------------------------------------------ bars
    def daily_bars(self, symbol: str, lookback_days: int = 60) -> list[Bar]:
        """Completed daily bars only — today's partial bar is excluded so all
        derived metrics are deterministic within a trading day."""
        cache_day = date.today()
        cached = self._bars_cache.get(symbol)
        if cached and cached[0] == cache_day:
            return cached[1]
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame

        self._throttle()
        try:
            resp = self._stocks.get_stock_bars(StockBarsRequest(
                symbol_or_symbols=[symbol],
                timeframe=TimeFrame.Day,
                start=utcnow() - timedelta(days=lookback_days * 2 + 10),
                end=utcnow() - timedelta(hours=12),  # skip today's partial bar
                adjustment="split",
            ))
        except Exception as exc:
            raise MarketDataError(f"bars fetch failed for {symbol}: {exc}") from exc
        bars: list[Bar] = []
        for b in _bars_list(resp, symbol):
            try:
                ts = getattr(b, "timestamp", None)
                td = ts.date() if hasattr(ts, "date") else ts
                bars.append(Bar(
                    trade_date=td,
                    open=float(b.open), high=float(b.high),
                    low=float(b.low), close=float(b.close),
                    volume=int(b.volume or 0),
                ))
            except Exception:
                continue
        bars.sort(key=lambda x: x.trade_date)
        self._bars_cache[symbol] = (cache_day, bars)
        return bars

    # ----------------------------------------------------------------- spot
    def latest_quote(self, symbol: str) -> StockQuote:
        from alpaca.data.requests import StockLatestQuoteRequest

        self._throttle()
        try:
            resp = self._stocks.get_stock_latest_quote(
                StockLatestQuoteRequest(symbol_or_symbols=[symbol])
            )
        except Exception as exc:
            raise MarketDataError(f"quote fetch failed for {symbol}: {exc}") from exc
        q = _mapping(resp, [symbol]).get(symbol)
        if q is None:
            raise MarketDataError(f"no quote returned for {symbol}")
        ts = getattr(q, "timestamp", None) or datetime.now().astimezone()
        return StockQuote(
            bid=float(getattr(q, "bid_price", 0) or 0) or None,
            ask=float(getattr(q, "ask_price", 0) or 0) or None,
            quoted_at=ts,
        )

    def spot(self, symbol: str) -> float:
        """Consolidated last price: latest trade, then quote mid, then last close."""
        from alpaca.data.requests import StockLatestTradeRequest

        self._throttle()
        try:
            resp = self._stocks.get_stock_latest_trade(
                StockLatestTradeRequest(symbol_or_symbols=[symbol])
            )
            trade = _mapping(resp, [symbol]).get(symbol)
            if trade is not None and getattr(trade, "price", None):
                return float(trade.price)
        except MarketDataError:
            raise
        except Exception:
            pass
        quote = self.latest_quote(symbol)
        if quote.mid:
            return float(quote.mid)
        bars = self.daily_bars(symbol)
        if bars:
            return bars[-1].close
        raise MarketDataError(f"no spot price available for {symbol}")

    def spy_returns(self) -> tuple[float | None, float | None]:
        bars = self.daily_bars("SPY", lookback_days=30)
        closes = [b.close for b in bars]
        if len(closes) < 7:
            return None, None
        r1 = closes[-1] / closes[-2] - 1
        r5 = closes[-1] / closes[-6] - 1
        return r1, r5

    # ------------------------------------------------------------- options
    def option_chain(self, symbol: str, expiry_lo: date, expiry_hi: date) -> list[Contract]:
        """All listed contracts for `symbol` with expirations in [lo, hi]."""
        cache_key = (symbol, expiry_lo, expiry_hi)
        cached = self._chain_cache.get(cache_key)
        if cached and (time.monotonic() - cached[0]) < CHAIN_TTL:
            return cached[1]
        from alpaca.trading.requests import GetOptionContractsRequest

        contracts: list[Contract] = []
        token = None
        for _page in range(8):
            self._throttle()
            try:
                req = GetOptionContractsRequest(
                    underlying_symbols=[symbol],
                    status="active",
                    expiration_date_gte=expiry_lo.isoformat(),
                    expiration_date_lte=expiry_hi.isoformat(),
                    limit=1000,
                )
                if token:
                    req.page_token = token
                resp = self._trading.get_option_contracts(req)
            except Exception as exc:
                raise MarketDataError(f"option chain fetch failed for {symbol}: {exc}") from exc
            for c in (getattr(resp, "option_contracts", None) or []):
                try:
                    exp = getattr(c, "expiration_date", None)
                    if hasattr(exp, "date"):
                        exp = exp.date()
                    contracts.append(Contract(
                        symbol=str(c.symbol),
                        strike=float(c.strike_price),
                        expiry=exp,
                        option_type=str(getattr(c, "type", "call")).lower(),
                    ))
                except Exception:
                    continue
            token = getattr(resp, "next_page_token", None)
            if not token:
                break
        # BRK.B and similar dot-names: Alpaca may require dash form
        if not contracts and "." in symbol:
            alt = symbol.replace(".", "-")
            return self.option_chain(alt, expiry_lo, expiry_hi)
        self._chain_cache[cache_key] = (time.monotonic(), contracts)
        return contracts

    def option_snapshots(self, contracts: list[Contract],
                         force: bool = False) -> dict[str, OptionQuote]:
        """Live OptionQuote per OCC symbol, with contract metadata coming from
        the chain objects. TTL 60s; force=True bypasses for the pre-submit
        re-quote (P4)."""
        by_symbol = {c.symbol: c for c in contracts}
        now = time.monotonic()
        result: dict[str, OptionQuote] = {}
        if not force:
            for s in by_symbol:
                cached = self._snap_cache.get(s)
                if cached and (now - cached[0]) < SNAP_TTL:
                    result[s] = cached[1]
        missing = [s for s in by_symbol if s not in result]
        if missing:
            from alpaca.data.requests import OptionSnapshotRequest

            for chunk_start in range(0, len(missing), 200):
                chunk = missing[chunk_start:chunk_start + 200]
                self._throttle()
                try:
                    resp = self._options.get_option_snapshot(
                        OptionSnapshotRequest(symbol_or_symbols=chunk)
                    )
                except Exception as exc:
                    raise MarketDataError(f"option snapshot fetch failed: {exc}") from exc
                fetched = datetime.now().astimezone()
                raw = _mapping(resp, chunk)
                for sym in chunk:
                    snap = raw.get(sym)
                    if snap is None:
                        continue
                    c = by_symbol[sym]
                    try:
                        lq = getattr(snap, "latest_quote", None)
                        bid = float(getattr(lq, "bid_price", 0) or 0) if lq else 0.0
                        ask = float(getattr(lq, "ask_price", 0) or 0) if lq else 0.0
                        qts = (getattr(lq, "timestamp", None) if lq else None) or fetched
                        iv = getattr(snap, "implied_volatility", None)
                        oi = getattr(snap, "open_interest", None) or 0
                        greeks = getattr(snap, "greeks", None)
                        delta = float(getattr(greeks, "delta", 0.0) or 0.0) if greeks else 0.0
                        q = OptionQuote(
                            option_symbol=sym, expiry=c.expiry, strike=c.strike,
                            option_type=c.option_type,  # type: ignore[arg-type]
                            bid=bid, ask=ask,
                            implied_vol=float(iv) if iv is not None else None,
                            open_interest=int(oi),
                            quoted_at=qts,
                        )
                        # _meta is not part of the pydantic model; we keep delta
                        # alongside in a side table for the structure desk.
                        self._deltas[sym] = delta
                        result[sym] = q
                        self._snap_cache[sym] = (time.monotonic(), q)
                    except Exception:
                        continue
        return {s: result[s] for s in by_symbol if s in result}

    def leg_delta(self, option_symbol: str) -> float:
        """Best-effort delta for a snapshot-fetched leg (0.0 if unknown)."""
        return self._deltas.get(option_symbol, 0.0)
