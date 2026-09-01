"""Central configuration: paths, secrets, and every risk parameter.

One source of truth for the numbers in ARCHITECTURE.md §3 and §5.6.
Settings.load() is the only entry point used by the orchestrator; it applies
the paper-only boot guard (P7) so a live-credential misconfiguration can never
even construct a usable Settings object.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PAPER_BASE = "https://paper-api.alpaca.markets"
LIVE_MARKER = "api.alpaca.markets"  # any non-paper host containing this is live


class LiveEndpointError(RuntimeError):
    """Raised when credentials/base URL resolve to a live-trading endpoint (P7)."""


def load_env_file(path: Path) -> None:
    """Minimal .env loader. Never overrides variables already in the environment."""
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


@dataclass(frozen=True)
class RiskParams:
    """All deterministic risk parameters (ARCHITECTURE §3.3, §3.4, §5.6)."""

    # -- sizing / concentration --
    max_loss_per_event_usd: float = 1_000.0
    max_aggregate_open_risk_usd: float = 4_000.0
    max_concurrent_events: int = 5
    max_entries_per_day: int = 2
    conviction_multipliers: dict[int, float] = field(
        default_factory=lambda: {5: 1.0, 4: 0.75, 3: 0.5}
    )

    # -- halts --
    day_pnl_restricted: float = -0.025
    day_pnl_halt: float = -0.050
    drawdown_halt: float = 0.08

    # -- universe / event window --
    event_window_min_days: int = 2
    event_window_max_days: int = 7
    price_min: float = 10.0
    price_max: float = 800.0
    min_dollar_vol_20d: float = 20_000_000.0

    # -- liquidity (per leg) --
    oi_min: int = 300
    spread_max_pct_of_mid: float = 0.15
    quote_max_age_minutes: int = 20

    # -- edge thresholds --
    move_ratio_runup_max: float = 0.95
    move_ratio_crush_min: float = 1.20
    em_hist_sample: int = 8
    em_hist_min: int = 4
    runup_min_abs_drift: float = 0.01
    runup_lookback_days: int = 5

    # -- structure construction --
    vertical_max_debit: float = 800.0
    vertical_long_delta: tuple[float, float] = (0.45, 0.55)
    vertical_short_delta: tuple[float, float] = (0.20, 0.25)
    condor_short_em_multiple: float = 1.10
    condor_min_wing_strikes: int = 2
    condor_max_wing_strikes: int = 4
    condor_min_credit: float = 1.00
    condor_min_credit_width_pct: float = 0.20

    # -- exits --
    vertical_profit_target_pct: float = 0.40
    vertical_stop_loss_pct: float = -0.35
    condor_profit_capture_pct: float = 0.55
    condor_mtm_loss_credit_mult: float = 1.6
    condor_mtm_loss_maxloss_pct: float = 0.75
    unfilled_close_cycles_before_perleg: int = 3

    # -- gates --
    price_sanity_band: tuple[float, float] = (0.80, 1.25)
    spy_intraday_max_drop: float = -0.015
    spy_5d_max_drop: float = -0.03
    min_expiry_days_after_exit: int = 4  # calendar days (proxy for 3 trading days)

    # -- llm pipeline --
    max_events_llm_per_cycle: int = 3
    news_headlines: int = 10

    def conviction_multiplier(self, conviction: int) -> float:
        """Conviction can only scale size DOWN (P6 spirit). <=2 means no trade."""
        return self.conviction_multipliers.get(conviction, 0.0)


@dataclass(frozen=True)
class Settings:
    repo_root: Path
    db_path: Path
    journal_path: Path
    dashboard_out: Path
    universe_path: Path

    alpaca_key_id: str
    alpaca_secret: str
    alpaca_base_url: str

    finnhub_key: str | None
    groq_key: str | None
    groq_model: str
    aiml_key: str | None
    aiml_model: str
    oai_compat_base_url: str | None
    oai_compat_key: str | None
    oai_compat_model: str

    discord_webhook: str | None
    allow_no_llm_entries: bool
    risk: RiskParams = field(default_factory=RiskParams)

    @classmethod
    def load(cls, repo_root: Path | None = None) -> "Settings":
        root = (repo_root or Path.cwd()).resolve()
        load_env_file(root / ".env")
        env = os.environ

        key = env.get("ALPACA_API_KEY_ID", "")
        secret = env.get("ALPACA_SECRET_KEY", "")
        base = env.get("ALPACA_BASE_URL", PAPER_BASE).rstrip("/")

        # P7: paper or nothing.
        if "paper" not in base and LIVE_MARKER in base:
            raise LiveEndpointError(
                f"ALPACA_BASE_URL={base!r} resolves to a LIVE endpoint. "
                "PrintRunner refuses to start against live credentials (P7). "
                f"Use {PAPER_BASE}."
            )

        return cls(
            repo_root=root,
            db_path=root / "data" / "printrunner.db",
            journal_path=root / "data" / "journal.jsonl",
            dashboard_out=root / "docs" / "index.html",
            universe_path=root / "config" / "universe.yaml",
            alpaca_key_id=key,
            alpaca_secret=secret,
            alpaca_base_url=base,
            finnhub_key=env.get("FINNHUB_API_KEY") or None,
            groq_key=env.get("GROQ_API_KEY") or None,
            groq_model=env.get("GROQ_MODEL", "llama-3.3-70b-versatile"),
            aiml_key=env.get("AIML_API_KEY") or None,
            aiml_model=env.get("AIML_MODEL", "gpt-4o-mini"),
            oai_compat_base_url=env.get("OPENAI_COMPAT_BASE_URL") or None,
            oai_compat_key=env.get("OPENAI_COMPAT_API_KEY") or None,
            oai_compat_model=env.get("OPENAI_COMPAT_MODEL", ""),
            discord_webhook=env.get("DISCORD_WEBHOOK_URL") or None,
            allow_no_llm_entries=env.get("ALLOW_NO_LLM_ENTRIES") == "1",
        )

    def load_universe(self) -> list[str]:
        """Hard-coded S&P 100 universe (P5: no drift; options_enabled checked daily)."""
        import yaml

        data = yaml.safe_load(self.universe_path.read_text())
        symbols = [str(s).strip().upper() for s in data["universe"]]
        if not symbols:
            raise ValueError("empty universe")
        return symbols

    def has_llm_provider(self) -> bool:
        return bool(self.groq_key or self.aiml_key or (self.oai_compat_base_url and self.oai_compat_key))
