"""Earnings calendar service (P5).

Sources: Finnhub bulk earnings-calendar (preferred; cheap, one call per
refresh) with per-symbol yfinance fallback. Every observation is point-in-time
with provenance; date changes are journaled as RESCHEDULE and the event is
blocked from entries until re-confirmed in two consecutive cycles
(confirm_cycles >= 1 after a reset).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

import httpx

from ..domain import EarningsEvent, utcnow
from ..state.state import StateDB
from ..journal.journal import Journal

FINNHUB_BASE = "https://finnhub.io/api/v1"
YF_CACHE_TTL = timedelta(hours=6)


class CalendarService:
    def __init__(self, settings, state: StateDB, journal: Journal) -> None:
        self.settings = settings
        self.state = state
        self.journal = journal
        self._yf_cache: dict[str, tuple[datetime, dict[str, Any]]] = {}

    # ------------------------------------------------------------ finnhub
    def _finnhub_upcoming(self, lo: date, hi: date) -> dict[str, tuple[date, str]]:
        key = self.settings.finnhub_key
        if not key:
            return {}
        try:
            resp = httpx.get(
                f"{FINNHUB_BASE}/calendar/earnings",
                params={
                    "from": lo.isoformat(),
                    "to": hi.isoformat(),
                    "token": key,
                },
                timeout=15.0,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            return {}
        items = data.get("earningsCalendar") or data.get("earnings") or []
        out: dict[str, tuple[date, str]] = {}
        for item in items:
            sym = str(item.get("symbol", "")).upper().replace(".", "-")
            raw_date = item.get("date")
            if not sym or not raw_date:
                continue
            try:
                d = date.fromisoformat(str(raw_date)[:10])
            except ValueError:
                continue
            hour = str(item.get("hour") or "").lower()
            timing = "BMO" if "bmo" in hour or "before" in hour else (
                "AMC" if "amc" in hour or "after" in hour else "UNSPECIFIED"
            )
            out[sym] = (d, timing)
        return out

    # ----------------------------------------------------------- yfinance
    def _yfinance_dates(self, symbol: str) -> dict[str, Any]:
        """Returns {"upcoming": date|None, "history": [date, ...]} or {}."""
        cached = self._yf_cache.get(symbol)
        if cached and cached[0] > utcnow():
            return cached[1]
        try:
            import yfinance as yf

            df = yf.Ticker(symbol).get_earnings_dates(limit=12)
            if df is None or df.empty:
                result: dict[str, Any] = {}
            else:
                dates = sorted(
                    (ts.date() if hasattr(ts, "date") else ts) for ts in df.index
                )
                today = utcnow().date()
                history = [d for d in dates if d < today][-8:]
                upcoming = next((d for d in dates if d >= today), None)
                result = {"upcoming": upcoming, "history": history}
        except Exception:
            result = {}
        self._yf_cache[symbol] = (utcnow() + YF_CACHE_TTL, result)
        return result

    # ------------------------------------------------------------- refresh
    def refresh(self, universe: list[str], today: date) -> list[EarningsEvent]:
        """Refresh calendar for the whole universe, detect reschedules,
        return events within the entry window."""
        rp = self.settings.risk
        lo, hi = today, today + timedelta(days=21)
        feed = self._finnhub_upcoming(lo, hi)

        # Normalize universe symbols to feed keys (BRK.B <-> BRK-B).
        uni_key = {s.upper().replace(".", "-"): s for s in universe}

        events: list[EarningsEvent] = []
        reschedules = 0
        missing_after_confirm: list[str] = []

        for feed_key, sym in uni_key.items():
            if feed_key in feed:
                event_date, timing = feed[feed_key]
                source = "finnhub"
            else:
                yf = self._yfinance_dates(sym)
                if yf.get("upcoming") and yf["upcoming"] <= hi:
                    event_date, timing, source = yf["upcoming"], "UNSPECIFIED", "yfinance"
                else:
                    # Date vanished while a confirmed future date sits in cache?
                    cached = self.state.cached_calendar(sym)
                    if cached and date.fromisoformat(cached["event_date"]) >= today:
                        missing_after_confirm.append(sym)
                    continue
            rescheduled_from, confirm = self.state.upsert_calendar(sym, event_date, timing, source)
            if rescheduled_from is not None:
                reschedules += 1
                self.journal.append("RESCHEDULE", {
                    "symbol": sym,
                    "old_date": rescheduled_from.isoformat(),
                    "new_date": event_date.isoformat(),
                    "note": "entry blocked until reconfirmed in 2 consecutive cycles",
                })
            events.append(EarningsEvent(
                symbol=sym, event_date=event_date, timing=timing,  # type: ignore[arg-type]
                source=source,  # type: ignore[arg-type]
                captured_at=utcnow(), rescheduled_from=rescheduled_from,
            ))

        self.journal.append("CALENDAR", {
            "universe": len(universe),
            "observed": len(events),
            "reschedules": reschedules,
            "missing_after_confirm": missing_after_confirm,
            "window": [lo.isoformat(), hi.isoformat()],
        })

        return [
            e for e in events
            if rp.event_window_min_days <= (e.event_date - today).days <= rp.event_window_max_days
        ]

    def earnings_history(self, symbol: str) -> list[date]:
        """Past announcement dates (<= 8) for EM-vs-history computation."""
        return list(self._yfinance_dates(symbol).get("history", []))
