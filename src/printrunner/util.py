"""Small shared utilities: NY-timezone time handling."""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")


def now_et() -> datetime:
    return datetime.now(NY)


def today_et() -> date:
    return now_et().date()
