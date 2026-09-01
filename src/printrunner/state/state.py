"""SQLite state store: positions, calendar cache, daily counters, kill-switch
latch, reviewer ban-list, LLM response cache, equity history.

The DB is derived state (reconstructable from broker + journal) except the
latch and counters, which are safety-critical and fail-closed: any error
reading them must stop trading, never continue on defaults.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime
from pathlib import Path

from ..domain import ExitPlan, HaltState, Position, PositionStatus, Structure
from ..domain import utcnow

_SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
    position_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    structure_json TEXT NOT NULL,
    entry_order_client_id TEXT NOT NULL,
    broker_order_id TEXT,
    status TEXT NOT NULL,
    filled_entry_price REAL,
    exit_plan_json TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    entry_notional REAL DEFAULT 0,
    close_attempts INTEGER DEFAULT 0,
    closed_at TEXT,
    realized_pnl REAL,
    exit_reason TEXT
);
CREATE TABLE IF NOT EXISTS calendar (
    symbol TEXT PRIMARY KEY,
    event_date TEXT NOT NULL,
    timing TEXT NOT NULL,
    source TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    rescheduled_from TEXT,
    confirm_cycles INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS counters (
    day TEXT PRIMARY KEY,
    entries INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS halt (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    tripped INTEGER NOT NULL DEFAULT 0,
    reason TEXT,
    tripped_at TEXT
);
CREATE TABLE IF NOT EXISTS orders (
    client_order_id TEXT PRIMARY KEY,
    position_id TEXT NOT NULL,
    role TEXT NOT NULL,
    submitted_at TEXT NOT NULL,
    status TEXT NOT NULL,
    broker_order_id TEXT,
    raw TEXT
);
CREATE TABLE IF NOT EXISTS bans (
    symbol TEXT PRIMARY KEY,
    reason TEXT NOT NULL,
    banned_until TEXT NOT NULL,
    added_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS llm_cache (
    cache_key TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    created_at TEXT NOT NULL,
    response_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS equity (
    day TEXT PRIMARY KEY,
    equity REAL NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS theses (
    position_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    thesis_json TEXT NOT NULL,
    thesis_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS hypotheses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    event_id TEXT NOT NULL,
    structure_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    regime_json TEXT NOT NULL,
    metrics_json TEXT NOT NULL,
    decision_json TEXT NOT NULL,
    thesis_json TEXT,
    outcome TEXT NOT NULL DEFAULT 'PENDING',
    pnl REAL,
    lesson TEXT,
    created_at TEXT NOT NULL
);
INSERT OR IGNORE INTO halt (id, tripped) VALUES (1, 0);
"""


class StateDB:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    # ------------------------------------------------------------------ halt
    def halt(self) -> HaltState:
        row = self.conn.execute("SELECT * FROM halt WHERE id=1").fetchone()
        if row is None:
            return HaltState()
        return HaltState(
            tripped=bool(row["tripped"]),
            reason=row["reason"],
            tripped_at=datetime.fromisoformat(row["tripped_at"]) if row["tripped_at"] else None,
        )

    def latch_halt(self, reason: str) -> HaltState:
        now = utcnow()
        self.conn.execute(
            "UPDATE halt SET tripped=1, reason=?, tripped_at=? WHERE id=1",
            (reason, now.isoformat()),
        )
        self.conn.commit()
        return self.halt()

    def clear_halt(self) -> None:
        self.conn.execute("UPDATE halt SET tripped=0, reason=NULL, tripped_at=NULL WHERE id=1")
        self.conn.commit()

    # ------------------------------------------------------------ positions
    def upsert_position(self, pos: Position) -> None:
        self.conn.execute(
            """INSERT INTO positions (
                position_id, event_id, symbol, structure_json,
                entry_order_client_id, broker_order_id, status,
                filled_entry_price, exit_plan_json, opened_at,
                entry_notional, close_attempts, closed_at, realized_pnl, exit_reason
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(position_id) DO UPDATE SET
                broker_order_id=excluded.broker_order_id,
                status=excluded.status,
                filled_entry_price=excluded.filled_entry_price,
                entry_notional=excluded.entry_notional,
                close_attempts=excluded.close_attempts,
                closed_at=excluded.closed_at,
                realized_pnl=excluded.realized_pnl,
                exit_reason=excluded.exit_reason""",
            (
                pos.position_id, pos.event_id, pos.symbol,
                pos.structure.model_dump_json(),
                pos.entry_order_client_id, pos.broker_order_id, pos.status.value,
                pos.filled_entry_price, pos.exit_plan.model_dump_json(),
                pos.opened_at.isoformat(), pos.entry_notional,
                pos.close_attempts, pos.closed_at.isoformat() if pos.closed_at else None,
                pos.realized_pnl, pos.exit_reason,
            ),
        )
        self.conn.commit()

    def _row_to_position(self, row: sqlite3.Row) -> Position:
        return Position(
            position_id=row["position_id"],
            event_id=row["event_id"],
            symbol=row["symbol"],
            structure=Structure.model_validate_json(row["structure_json"]),
            entry_order_client_id=row["entry_order_client_id"],
            broker_order_id=row["broker_order_id"],
            status=PositionStatus(row["status"]),
            filled_entry_price=row["filled_entry_price"],
            exit_plan=ExitPlan.model_validate_json(row["exit_plan_json"]),
            opened_at=datetime.fromisoformat(row["opened_at"]),
            entry_notional=row["entry_notional"],
            close_attempts=row["close_attempts"],
            closed_at=datetime.fromisoformat(row["closed_at"]) if row["closed_at"] else None,
            realized_pnl=row["realized_pnl"],
            exit_reason=row["exit_reason"],
        )

    def open_positions(self) -> list[Position]:
        rows = self.conn.execute(
            "SELECT * FROM positions WHERE status NOT IN ('CLOSED','REJECTED')"
        ).fetchall()
        return [self._row_to_position(r) for r in rows]

    def all_positions(self) -> list[Position]:
        rows = self.conn.execute("SELECT * FROM positions ORDER BY opened_at").fetchall()
        return [self._row_to_position(r) for r in rows]

    def position(self, position_id: str) -> Position | None:
        row = self.conn.execute(
            "SELECT * FROM positions WHERE position_id=?", (position_id,)
        ).fetchone()
        return self._row_to_position(row) if row else None

    def aggregate_open_risk(self) -> float:
        return sum(p.open_risk_usd for p in self.open_positions())

    def has_event_position(self, event_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM positions WHERE event_id=? AND status NOT IN ('CLOSED','REJECTED')",
            (event_id,),
        ).fetchone()
        return row is not None

    # --------------------------------------------------------------- orders
    def add_order(self, client_order_id: str, position_id: str, role: str,
                  broker_order_id: str | None = None) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO orders (client_order_id, position_id, role, "
            "submitted_at, status, broker_order_id) VALUES (?,?,?,?,?,?)",
            (client_order_id, position_id, role, utcnow().isoformat(),
             "SUBMITTED", broker_order_id),
        )
        self.conn.commit()

    def update_order_status(self, client_order_id: str, status: str) -> None:
        self.conn.execute(
            "UPDATE orders SET status=? WHERE client_order_id=?", (status, client_order_id)
        )
        self.conn.commit()

    def orders_for_position(self, position_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM orders WHERE position_id=? ORDER BY submitted_at",
            (position_id,),
        ).fetchall()

    def entry_orders_pending_broker_check(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM orders WHERE role='ENTRY' AND status IN ('SUBMITTED','FILLED')"
        ).fetchall()

    # ------------------------------------------------------------- calendar
    def upsert_calendar(self, symbol: str, event_date: date, timing: str,
                        source: str) -> tuple[date | None, int]:
        """Returns (rescheduled_from, confirm_cycles). confirm_cycles counts
        consecutive refreshes that agreed on the same date; resets to 0 on any
        date change. Entries require confirm_cycles >= 1 (P5)."""
        prev = self.cached_calendar(symbol)
        rescheduled_from: date | None = None
        confirm = 0
        if prev is not None:
            prev_date = date.fromisoformat(prev["event_date"])
            if prev_date == event_date:
                confirm = int(prev["confirm_cycles"] or 0) + 1
            else:
                rescheduled_from = prev_date
        self.conn.execute(
            """INSERT INTO calendar (symbol, event_date, timing, source, captured_at,
                                     rescheduled_from, confirm_cycles)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(symbol) DO UPDATE SET
                 event_date=excluded.event_date,
                 timing=excluded.timing,
                 source=excluded.source,
                 captured_at=excluded.captured_at,
                 rescheduled_from=excluded.rescheduled_from,
                 confirm_cycles=excluded.confirm_cycles""",
            (symbol, event_date.isoformat(), timing, source,
             utcnow().isoformat(), rescheduled_from.isoformat() if rescheduled_from else None,
             confirm),
        )
        self.conn.commit()
        return rescheduled_from, confirm

    def cached_calendar(self, symbol: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM calendar WHERE symbol=?", (symbol,)
        ).fetchone()

    # ------------------------------------------------------------- counters
    def entries_today(self, day: date) -> int:
        row = self.conn.execute(
            "SELECT entries FROM counters WHERE day=?", (day.isoformat(),)
        ).fetchone()
        return int(row["entries"]) if row else 0

    def incr_entries(self, day: date) -> None:
        self.conn.execute(
            "INSERT INTO counters (day, entries) VALUES (?,1) "
            "ON CONFLICT(day) DO UPDATE SET entries = entries + 1",
            (day.isoformat(),),
        )
        self.conn.commit()

    # ----------------------------------------------------------------- bans
    def add_ban(self, symbol: str, reason: str, banned_until: date) -> None:
        self.conn.execute(
            """INSERT INTO bans (symbol, reason, banned_until, added_at) VALUES (?,?,?,?)
               ON CONFLICT(symbol) DO UPDATE SET
                 reason=excluded.reason, banned_until=excluded.banned_until""",
            (symbol, reason, banned_until.isoformat(), utcnow().isoformat()),
        )
        self.conn.commit()

    def active_bans(self, today: date) -> dict[str, str]:
        rows = self.conn.execute("SELECT * FROM bans").fetchall()
        return {
            r["symbol"]: r["reason"]
            for r in rows
            if date.fromisoformat(r["banned_until"]) >= today
        }

    # ------------------------------------------------------------ llm cache
    def llm_cache_get(self, cache_key: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM llm_cache WHERE cache_key=?", (cache_key,)
        ).fetchone()
        return json.loads(row["response_json"]) if row else None

    def llm_cache_put(self, cache_key: str, provider: str, model: str, response: dict) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO llm_cache (cache_key, provider, model, created_at, response_json) "
            "VALUES (?,?,?,?,?)",
            (cache_key, provider, model, utcnow().isoformat(), json.dumps(response)),
        )
        self.conn.commit()

    # --------------------------------------------------------------- equity
    def set_equity(self, day: date, equity: float) -> None:
        self.conn.execute(
            "INSERT INTO equity (day, equity, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(day) DO UPDATE SET equity=excluded.equity, updated_at=excluded.updated_at",
            (day.isoformat(), equity, utcnow().isoformat()),
        )
        self.conn.commit()

    def equity_today(self, day: date) -> float | None:
        row = self.conn.execute(
            "SELECT equity FROM equity WHERE day=?", (day.isoformat(),)
        ).fetchone()
        return float(row["equity"]) if row else None

    def peak_equity(self) -> float | None:
        row = self.conn.execute("SELECT MAX(equity) AS peak FROM equity").fetchone()
        return float(row["peak"]) if row and row["peak"] is not None else None

    def equity_history(self) -> list[tuple[date, float]]:
        rows = self.conn.execute("SELECT day, equity FROM equity ORDER BY day").fetchall()
        return [(date.fromisoformat(r["day"]), float(r["equity"])) for r in rows]

    # -------------------------------------------------------------- thesis
    def put_thesis(self, position_id: str, event_id: str, symbol: str, thesis_json: str, thesis_hash: str) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO theses (position_id, event_id, symbol, thesis_json, thesis_hash, created_at) VALUES (?,?,?,?,?,?)",
            (position_id, event_id, symbol, thesis_json, thesis_hash, utcnow().isoformat()),
        )
        self.conn.commit()

    def get_thesis(self, position_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM theses WHERE position_id=?", (position_id,)).fetchone()

    # ---------------------------------------------------------- hypotheses
    def add_hypothesis(self, symbol: str, event_id: str, structure_id: str, kind: str,
                       regime: dict, metrics: dict, decision: dict, thesis: dict | None = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO hypotheses (symbol, event_id, structure_id, kind, regime_json, metrics_json, decision_json, thesis_json, outcome, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (symbol, event_id, structure_id, kind, json.dumps(regime), json.dumps(metrics), json.dumps(decision), json.dumps(thesis) if thesis else None, "PENDING", utcnow().isoformat()),
        )
        self.conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    def update_hypothesis(self, hyp_id: int, outcome: str, pnl: float | None = None, lesson: str | None = None) -> None:
        self.conn.execute("UPDATE hypotheses SET outcome=?, pnl=?, lesson=? WHERE id=?", (outcome, pnl, lesson, hyp_id))
        self.conn.commit()

    def recent_hypotheses(self, symbol: str | None = None, limit: int = 20) -> list[sqlite3.Row]:
        if symbol:
            return self.conn.execute("SELECT * FROM hypotheses WHERE symbol=? ORDER BY id DESC LIMIT ?", (symbol, limit)).fetchall()
        return self.conn.execute("SELECT * FROM hypotheses ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    def similar_hypotheses(self, regime: dict, symbol: str | None = None, limit: int = 3) -> list[sqlite3.Row]:
        """Cheap regime similarity: cosine on normalized vector [move_ratio, vrp, drift, spy5d]."""
        import math
        def vec(r: dict) -> list[float]:
            return [float(r.get("move_ratio") or 0), float(r.get("vrp") or 0), float(r.get("drift") or 0), float(r.get("spy5d") or 0)]
        target = vec(regime)
        tn = math.sqrt(sum(x*x for x in target)) or 1.0
        target = [x/tn for x in target]
        cands = self.recent_hypotheses(symbol=symbol, limit=50) if symbol else self.recent_hypotheses(limit=50)
        scored: list[tuple[float, sqlite3.Row]] = []
        for row in cands:
            try:
                r = json.loads(row["regime_json"])
                v = vec(r)
                vn = math.sqrt(sum(x*x for x in v)) or 1.0
                v = [x/vn for x in v]
                cos = sum(a*b for a, b in zip(target, v))
                # boost failures — negative results are most valuable (post)
                if row["outcome"] in ("REJECTED", "BREAKER_KILL", "CLOSED") and (row["pnl"] or 0) < 0:
                    cos += 0.15
                scored.append((cos, row))
            except Exception:
                continue
        scored.sort(key=lambda x: x[0], reverse=True)
        return [r for _, r in scored[:limit]]
