"""Shared domain contracts (pydantic). These types are the interface between
every subsystem: calendar -> marketdata -> screener -> risk -> llm -> execution
-> positions -> journal. Keeping them in one module prevents drift."""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, computed_field


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def stable_hash(payload: Any) -> str:
    """Deterministic short hash of any JSON-serializable payload."""
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


# ---------------------------------------------------------------- calendar ---


class EarningsEvent(BaseModel):
    """Point-in-time record of an earnings date, with provenance (P5)."""

    symbol: str
    event_date: date  # announcement date ("D-day")
    timing: Literal["BMO", "AMC", "UNSPECIFIED"] = "UNSPECIFIED"
    source: Literal["finnhub", "yfinance", "operator"]  # operator = human-directed waiver entry
    captured_at: datetime
    rescheduled_from: date | None = None  # set when we saw a different date earlier

    @computed_field  # type: ignore[prop-decorator]
    @property
    def event_id(self) -> str:
        return f"{self.symbol}:{self.event_date.isoformat()}"


# ------------------------------------------------------------- market data ---


class StockQuote(BaseModel):
    bid: float | None = None
    ask: float | None = None
    last: float | None = None
    quoted_at: datetime
    source: str = "alpaca"

    @property
    def mid(self) -> float | None:
        if self.bid is not None and self.ask is not None:
            return (self.bid + self.ask) / 2
        return self.last


class OptionQuote(BaseModel):
    option_symbol: str  # OCC-22
    expiry: date
    strike: float
    option_type: Literal["call", "put"]
    bid: float
    ask: float
    implied_vol: float | None = None
    open_interest: int = 0
    volume: int = 0
    quoted_at: datetime

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2


class MarketDataSnapshot(BaseModel):
    """All raw inputs for one symbol at one epoch. Validation target for
    consistency checks (fresh quotes, sane prices) before ANY derived use."""

    symbol: str
    fetched_at: datetime
    spot: float  # consolidated last/close used consistently everywhere
    quote: StockQuote
    chain: list[OptionQuote] = Field(default_factory=list)
    spy_spot: float
    spy_quote: StockQuote
    spy_ret_1d: float | None = None
    spy_ret_5d: float | None = None
    iv_rank: float | None = None  # 0..100, options-chain derived
    hv20: float | None = None  # annualized realized 20d
    hist_earn_moves: list[float] = Field(default_factory=list)  # abs % moves, last 8
    closes_recent: list[float] = Field(default_factory=list)  # ~last 6 closes
    news_headlines: list[str] = Field(default_factory=list)
    fetch_errors: list[str] = Field(default_factory=list)


class Metrics(BaseModel):
    """Deterministic metrics — ALWAYS computed from a validated snapshot by
    screener.compute_metrics. Never from LLM output, never from stale cache."""

    expected_move_pct: float  # EM/S as fraction
    expected_move_usd: float
    move_ratio: float  # EM vs avg abs historical earnings move
    hist_move_sample: int
    runup_drift: float  # return over runup_lookback_days
    vrp: float | None = None  # straddle IV - hv20 (same-expiry where possible)
    iv_rank: float | None = None
    spy_ret_1d: float | None = None
    spy_ret_5d: float | None = None


# --------------------------------------------------------------- structure ---


class Leg(BaseModel):
    option_symbol: str
    side: Literal["buy", "sell"]
    ratio: int = 1
    quote_at_selection: OptionQuote


class StructureKind(str, Enum):
    CALL_DEBIT_VERTICAL = "call_debit_vertical"
    PUT_DEBIT_VERTICAL = "put_debit_vertical"
    IRON_CONDOR = "iron_condor"


class Structure(BaseModel):
    """A defined-risk candidate built by the structure desk from real quotes.
    max_loss/max_profit are computed by code from quotes — never asserted by LLM."""

    structure_id: str
    kind: StructureKind
    legs: list[Leg]
    expires_on: date
    contracts: int = 1  # set at sizing time
    max_loss_per_contract: float  # > 0 (debit or margin-required credit)
    max_profit_per_contract: float  # >= 0
    breakevens: list[float]
    entry_cost_per_contract: float  # debit (positive) or credit (negative)
    label: str  # human-readable, e.g. "AAPL 245/255C Jul19"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_credit(self) -> bool:
        return self.entry_cost_per_contract < 0


class GateCode(str, Enum):
    G1 = "G1_missing_data"
    G2 = "G2_stale_quotes"
    G3 = "G3_price_sanity"
    G4 = "G4_structure_price_inconsistent"
    G5 = "G5_expiry_window"
    G6 = "G6_liquidity"
    G7 = "G7_risk_budget"
    G8 = "G8_market_shock"
    G9 = "G9_runup_favorable"
    G10 = "G10_edge_missing"


class GateOutcome(BaseModel):
    structure_id: str
    passed: bool
    failures: list[tuple[GateCode, str]] = Field(default_factory=list)
    contracts: int = 0
    max_loss_usd: float = 0.0


# -------------------------------------------------------------- llm/decision ---


class Decision(BaseModel):
    """The ONLY artifact the LLM is allowed to produce. Validated hard:
    candidate_id must exist in the shortlist it was shown; conviction in [3,5];
    any deviation -> discarded as hallucination (P1)."""

    candidate_id: str | None = None
    action: Literal["SELECT", "DECLINE_ALL"] = "DECLINE_ALL"
    conviction: int = Field(ge=1, le=5)
    rationale: str = ""
    considered: list[str] = Field(default_factory=list)

    @property
    def tradable(self) -> bool:
        return self.action == "SELECT" and self.candidate_id is not None and self.conviction >= 3


class CandidateBrief(BaseModel):
    """What we show the LLM: identifiers + facts. No instructions to act."""

    candidate_id: str
    kind: str
    label: str
    expires_on: date
    max_loss_per_contract: float
    max_profit_per_contract: float
    breakevens: list[float]
    entry_cost_per_contract: float


# --------------------------------------------------------------- positions ---


class ExitPlan(BaseModel):
    """Deterministic exit rules, computed at entry by code (never by LLM)."""

    profit_target_pct: float  # fraction of entry debit (verticals)
    stop_loss_pct: float  # fraction of entry debit (verticals), negative
    credit_capture_pct: float | None = None  # condors: fraction of credit
    condor_mtm_stop: float | None = None  # condors: mtm loss as mult of credit


class PositionStatus(str, Enum):
    PENDING_FILL = "PENDING_FILL"
    OPEN = "OPEN"
    CLOSE_SUBMITTED = "CLOSE_SUBMITTED"
    CLOSED = "CLOSED"
    REJECTED = "REJECTED"


class Position(BaseModel):
    position_id: str
    event_id: str
    symbol: str
    structure: Structure  # full snapshot at entry (auditable)
    entry_order_client_id: str
    broker_order_id: str | None = None
    status: PositionStatus = PositionStatus.PENDING_FILL
    filled_entry_price: float | None = None  # per contract, from broker fill
    exit_plan: ExitPlan
    opened_at: datetime
    entry_notional: float = 0.0  # filled debit/credit * contracts (credit < 0)
    close_attempts: int = 0
    closed_at: datetime | None = None
    realized_pnl: float | None = None
    exit_reason: str | None = None

    @property
    def open_risk_usd(self) -> float:
        """Aggregate max loss still at risk (0 once closed)."""
        if self.status in (PositionStatus.CLOSED, PositionStatus.REJECTED):
            return 0.0
        return self.structure.max_loss_per_contract * self.structure.contracts


class HaltState(BaseModel):
    """Kill-switch latch: once tripped, stays tripped until a human resumes."""

    tripped: bool = False
    reason: str | None = None
    tripped_at: datetime | None = None


# -------------------------------------------------------------- loop thesis ---


class Thesis(BaseModel):
    """Preregistered expected outcome — immutable once written (post's trick)."""

    position_id: str
    event_id: str
    symbol: str
    structure_id: str
    expected_move_pct: float
    expected_hold_days: int
    invalidation: str  # e.g. "spot > short_call+wing OR vrp<0"
    expected_pnl_pct: float
    created_at: datetime
    thesis_hash: str = ""


class Hypothesis(BaseModel):
    """Persistent world-model node — remembers what was tested and where it bled."""

    symbol: str
    event_id: str
    structure_id: str
    kind: str
    regime: dict  # {move_ratio, vrp, drift, spy_ret, hv, iv_rank}
    metrics: dict
    decision: dict
    thesis: dict | None = None
    outcome: str = "PENDING"  # PENDING | FILLED | CLOSED | REJECTED | BREAKER_KILL
    pnl: float | None = None
    lesson: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
