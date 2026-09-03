# PrintRunner

*An autonomous, auditable agent that trades defined-risk options structures around earnings announcements on **Alpaca paper** — built on the "LLM least-trusted" pattern: the model analyzes and picks from a deterministic shortlist, but every price, structure, size, and exit is computed and enforced by code it cannot override.*

[![Tests](https://img.shields.io/badge/tests-14%2F14-brightgreen)](#testing) [![Paper only](https://img.shields.io/badge/trading-paper_only-blue)](#safety) [![License](https://img.shields.io/badge/license-MIT-green)](#license)

This is the build for the [Alpaca AI Trading Agents Hackathon](https://lablab.ai/event/alpaca-ai-trading-agents-hackathon) — track **Options Alpha Agents**. It implements the full Earnings Season Agent architecture sketched before build.

## Demo

- **Video (2:28, edge-tts + ffmpeg + PIL)**: [`docs/video/PrintRunner.mp4`](docs/video/PrintRunner.mp4) — also hosted at `https://printrunner.vercel.app/video/PrintRunner.mp4`
- **Slides (10, reportlab, punchy)**: [`docs/PrintRunner.pdf`](docs/PrintRunner.pdf) — `https://printrunner.vercel.app/PrintRunner.pdf`
- **Live dashboard**: `https://printrunner.vercel.app/` (fetches Supabase at runtime — no rebuild or commit per run; keys are injected from `.env` at build time and never committed)

---

## 1 · Design laws

| # | Law | Where it lives |
|---|---|---|
| P1 | LLM is the least-trusted component — it only *selects from* a validated shortlist | `src/printrunner/llm/team.py` hallucination guard |
| P2 | Defined-risk only, structurally — every position is one `mleg` order, max loss known | `src/printrunner/screener/desk.py` |
| P3 | Fail closed — uncertainty means "don't trade" | every `GateOutcome`, `evaluate_exit` "cannot value → CLOSE" |
| P4 | Restart can't hurt — deterministic `client_order_id` + reconcile-before-write | `src/printrunner/execution/broker.py` |
| P5 | Point-in-time honest — calendar provenance + `confirm_cycles >= 1` before entry | `src/printrunner/calendar/service.py`, `state/state.py` |
| P6 | Adapt only toward restriction — reviewer only adds bans | `src/printrunner/llm/team.py:reviewer_bans` |
| P7 | Paper or nothing — boot guard refuses live credentials | `src/printrunner/config.py:Settings.load` |
| P8 | The agent earns trust by saying NO — every rejection is journaled | `data/journal.jsonl` hash chain |

---

## 2 · Strategy

Two edges, both deterministic to detect:

| Family | Anomaly | Detection | Expression |
|---|---|---|---|
| **RUNUP** | Pre-earnings announcement drift | `MoveRatio ≤ 0.95` (EM cheap vs history) | Debit vertical, entry T−5..T−2, exits before the print |
| **CRUSH** | Vol risk premium into earnings | `MoveRatio ≥ 1.20` and `VRP > 0` | Short iron condor, entry T−2..T−1, held through the print |

Core metrics (all from validated snapshot, never LLM output):

- **EM** — ATM straddle mid for the first expiry spanning the event, / spot
- **EM vs history** — `EM% / mean(|last 8 earnings moves|)` → `MoveRatio`
- **Runup drift** — 5-trading-day return into the print
- **VRP** — straddle IV (solved via Black-Scholes bisection) − 20-day annualized HV
- Stale quotes, wide spreads, low OI all fail gates before sizing.

---

## 3 · System

```
GitHub Actions cron (*/20 min, 13-20 UTC, weekdays)
        │
  ┌─────▼─────┐  events   ┌──────────────┐  briefs  ┌──────────────┐
  │ CALENDAR  ├──────────►│ ORCHESTRATOR ├─────────►│   LLM TEAM   │
  │ Finnhub/  │           │ 9-step cycle │◄─────────┤ Groq → AIML  │
  │ yfinance  │           └─┬──┬──┬──┬───┘ Decision │ → Compat     │
  └───────────┘             │  │  │  │   JSON       └──────────────┘
                            │  │  │  └──────────────┐
  ┌──────────────────┐      │  │  │                 │
  │ MARKET DATA      │◄─────┘  │  │   ┌─────────────▼─────────┐
  │ Alpaca (cached)  │         │  │   │  RISK GATES G1-G10    │
  └──────────────────┘         │  │   │  + conviction sizing  │
                               │  │   └──────┬────────────────┘
  ┌──────────────────┐         │  │          │ pass
  │ SCREENER         │◄────────┘  │   ┌──────▼───────────────┐
  │ metrics + desk   │ candidates │   │  EXECUTION (mleg)    │
  └──────────────────┘            │   │  deterministic id,   │
                                  │   │  fail-closed submit  │
  ┌──────────────────┐            │   └──────┬───────────────┘
  │ POSITION MGR     │◄───────────┘          │
  │ exit engine      │                       ▼
  └────────┬─────────┘              Alpaca Trading API (paper)
           │ close orders                    │
  ┌────────▼────────┐   reconcile   ┌────────▼─────────┐
  │ JOURNAL (hash-  │◄──────────────┤  RECONCILER      │
  │ chained JSONL)  │  drift→HALT   │  + kill switch   │
  └────────┬────────┘               └──────────────────┘
           │ artifacts
  ┌────────▼────────┐       ┌──────────────────┐
  │ DASHBOARD       │       │ REVIEWER (bans)  │
  │ docs/index.html │       └──────────────────┘
  └─────────────────┘
```

### Risk gates G1–G10

| Code | Gate | What it checks |
|---|---|---|
| G1 | missing data | spot/chain/hist sample present |
| G2 | stale quotes | leg `quoted_at` ≤ 20 min, stock quote ≤ 30 min |
| G3 | price sanity | spot in `[0.80, 1.25]×` ref, bid ≤ ask |
| G4 | structure price | debit/credit sign, `debit < width`, `max_loss > 0` |
| G5 | expiry window | `4 ≤ (expiry − event) ≤ 10` days, DTE ≥ 4 |
| G6 | liquidity | OI ≥ 300, spread ≤ 15% of mid |
| G7 | risk budget | per-event ≤ $1k, aggregate ≤ $4k, concurrent < 5, ≤ 2/day, buying power, conviction 3-5 |
| G8 | market shock | SPY 1d > −1.5%, 5d > −3%, halt not latched, market open |
| G9 | runup favorable | vertical `drift > −1%`, condor `drift > +1%` |
| G10 | edge | vertical `MoveRatio ≤ 0.95`, condor `MoveRatio ≥ 1.20` and `VRP > 0` |

Gates are re-evaluated on **fresh quotes** immediately before submission (P4).

Exit engine: verticals target +40% / stop −35% / DTE<4 & pnl<20% → close; condors capture 55% of credit / MTM stop `min(1.6×credit, 75% max loss)` / DTE≤2 / wing breach → close. "Cannot value" → close.

---

## 4 · Quickstart

```bash
uv sync
cp .env.example .env   # fill ALPACA_API_KEY_ID, ALPACA_SECRET_KEY (paper), FINNHUB_API_KEY, GROQ_API_KEY, ...
uv run pr status       # verify halt/journal/db
uv run pr cycle        # one full cycle (reconcile → exits → calendar → screen → LLM → gates → execute)
uv run pr dashboard    # rebuild docs/index.html (gitignored; needs SUPABASE_* in .env for live fetch)
uv run pr journal -n 30
uv run pr journal --verify
```

### CLI

```
pr status              # halt, journal chain, open positions, aggregate risk
pr cycle               # run one 9-step cycle
pr journal [-n N]      # tail journal
pr journal --verify    # hash-chain verify
pr resume --confirm ACK# clear a latched HALT (manual only)
pr dashboard           # rebuild static dashboard
```

`pr cycle` is idempotent — rerunning the same cycle is a no-op due to deterministic `client_order_id`s.

### Env

See `.env.example`. Trading credentials **must** point at `https://paper-api.alpaca.markets` (Alpaca docs show `https://paper-api.alpaca.markets/v2` as the REST prefix — `Settings.load` at `src/printrunner/config.py:141` strips a trailing `/v2` so both forms work; your paper key/secret are for paper, and double `/v2/v2` is normalized). Any live host is refused at `Settings.load` (P7).

LLM providers are tried in order Groq → AIML → OpenAI-compatible custom endpoint. Missing keys are skipped; with no provider the agent is fail-closed for *entries* (exits still run). Set `ALLOW_NO_LLM_ENTRIES=1` only for dry runs.

---

## 5 · Deployment

No commits per run. The loop is **Cloudflare Worker → Actions → Supabase → Vercel**:

- A Cloudflare Worker (`worker/`, cron `*/20 13-20 * * 1-5` weekdays) dispatches `.github/workflows/cycle.yml` (`workflow_dispatch` only) via the GitHub API. Every `Journal.append` is mirrored to Supabase (`src/printrunner/supabase/`, fail-open).
- The dashboard (`src/printrunner/dashboard/build.py`) is a static shell that fetches `journal`/`equity` from Supabase **in the browser at runtime** — no rebuild or redeploy per run. Build locally with `uv run pr dashboard` (reads `SUPABASE_URL`/`SUPABASE_ANON_KEY` from `.env`), then publish with `vercel deploy` from `docs/`. `docs/index.html` is gitignored precisely because the built file carries the public anon key; **never `git add` it**.
- Live: `https://printrunner.vercel.app/`

Required Actions secrets: `ALPACA_API_KEY_ID`, `ALPACA_SECRET_KEY`, `ALPACA_BASE_URL` (paper), `FINNHUB_API_KEY`, `GROQ_API_KEY` (and optionally `AIML_API_KEY`, `OPENAI_COMPAT_*`, `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`, `SUPABASE_ANON_KEY`, `DISCORD_WEBHOOK_URL`).

---

## 6 · Safety & disclosures

- EM from the spanning straddle overestimates by including non-event variance.
- VRP is a realized-vs-implied proxy (no IV history on the free feed).
- Option marks come from the delayed Indicative feed on the free plan.
- The earnings calendar is third-party (Finnhub primary, yfinance fallback) — provenance and reschedule history are journaled.
- There is no options backtest in this repo: free historical chains for our window don't exist. Results are **forward paper only**. One contest week proves nothing about profitability. The claim is *auditable process around a documented event edge*.

Short legs are always covered by a same-expiry long (GCD ratios = 1, every short paired). No naked shorts are representable.

---

## 7 · Testing

Pure-logic units with no network (bars/quotes are synthetic):

```bash
uv run pytest -q          # 14 tests: BS round-trip, metrics, desk, gates, journal chain, state latch, exits, LLM guard, sizing
uv run pytest -v
```

`tests/test_core.py` covers: Black-Scholes round-trip & delta bounds, straddle IV, expiry selection, EM/MoveRatio, insufficient-history G1, vertical construction, stale/low-OI gates, deterministic `client_order_id`, signed limit convention, exit target evaluation, hallucination guard, journal tamper detection, halt latch.

---

## 8 · Repo layout

```
config/universe.yaml          # S&P 100 (hard-coded, P5)
src/printrunner/
  config.py                   # Settings + RiskParams (single source of truth)
  domain.py                   # pydantic contracts
  util.py                     # NY-timezone helpers
  journal/journal.py           # hash-chained JSONL
  state/state.py               # SQLite: positions, calendar, halt, bans, equity
  marketdata/{alpaca,bs,news}.py
  calendar/service.py          # Finnhub + yfinance, confirm_cycles
  screener/{metrics,desk}.py   # EM/MoveRatio/VRP + structure construction
  risk/gates.py                # G1-G10 + conviction sizing
  execution/broker.py          # mleg builder, deterministic id, fail-closed
  positions/exits.py           # exit plans + evaluation
  reconcile/reconciler.py      # broker↔local diff, latched HALT
  llm/team.py                  # Groq→AIML→compat, JSON, cache, reviewer bans
  orchestrator/cycle.py        # 9-step pipeline
  dashboard/build.py           # static docs/index.html
  cli.py                       # pr entry point
tests/test_core.py
.github/workflows/cycle.yml
docs/.nojekyll + docs/index.html (generated)
```

---

## 9 · License

MIT — see [LICENSE](LICENSE).
