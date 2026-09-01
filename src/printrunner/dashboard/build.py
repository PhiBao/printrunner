"""Static dashboard builder — reads journal + state DB, writes docs/index.html."""

from __future__ import annotations

import json
from pathlib import Path

from ..config import Settings
from ..journal.journal import Journal
from ..state.state import StateDB
from ..util import today_et


_HTML = """<!doctype html>
<meta charset="utf-8">
<title>PrintRunner — paper dashboard</title>
<style>
 body{{font-family:ui-monospace,monospace;max-width:980px;margin:24px auto;padding:0 16px;color:#111}}
 h1{{font-size:20px}} h2{{font-size:16px;margin-top:28px;border-bottom:1px solid #ddd;padding-bottom:6px}}
 table{{border-collapse:collapse;width:100%}} th,td{{border:1px solid #ddd;padding:6px 8px;font-size:12px;text-align:left}}
 .halt{{color:#b00;font-weight:700}} .ok{{color:#080}} pre{{white-space:pre-wrap;background:#f6f6f6;padding:12px;border-radius:8px}}
 .loop{{background:#eef7ff;border:1px solid #b6d4fe;padding:10px;border-radius:8px;margin:14px 0}}
</style>
<h1>PrintRunner <small>paper dashboard — closed loop</small></h1>
<p>Today: {today} · halt: <span class="{halt_cls}">{halt}</span> · entries today: {entries}</p>
<div class="loop"><b>Loop:</b> Research → Code → Backtest(breaker) → Live → Post-mortem(autopsy) → Fine-tune(hypothesis graph) · hypotheses {n_hyp} · theses {n_thesis} · breaker kills {n_breaker}</div>
<h2>Open positions ({n_open}) — aggregate risk ${agg:.0f}</h2>
{positions_table}
<h2>Theses (preregistered, immutable)</h2>
{theses_table}
<h2>Hypothesis graph (recent 10 — negative results are most valuable)</h2>
{hyp_table}
<h2>Breaker kills (2x costs)</h2>
<pre>{breaker_tail}</pre>
<h2>Active bans</h2>
{bans_table}
<h2>Recent journal (last 30)</h2>
<pre>{journal_tail}</pre>
<h2>Equity history</h2>
<pre>{equity}</pre>
<p><small>Built from data/journal.jsonl + data/printrunner.db. Rebuilt every cycle. P8 hash chain verified at boot. Breaker at 2x costs / worst regimes. Swarm specialists hide price until aggregator.</small></p>
"""


def build_dashboard(settings: Settings) -> Path:
    out = settings.dashboard_out
    out.parent.mkdir(parents=True, exist_ok=True)
    state = StateDB(settings.db_path)
    journal = Journal(settings.journal_path)
    today = today_et()
    halt = state.halt()
    halt_txt = f"{halt.tripped} — {halt.reason or ''}".strip()
    halt_cls = "halt" if halt.tripped else "ok"
    positions = state.open_positions()
    if positions:
        rows = "".join(
            f"<tr><td>{p.symbol}</td><td>{p.structure.kind.value}</td><td>{p.structure.label}</td>"
            f"<td>{p.status.value}</td><td>{p.structure.contracts}×</td><td>{p.structure.max_loss_per_contract:.0f}/c</td>"
            f"<td>{p.event_id}</td></tr>"
            for p in positions
        )
        positions_table = f"<table><tr><th>symbol</th><th>kind</th><th>label</th><th>status</th><th>qty</th><th>max loss/c</th><th>event</th></tr>{rows}</table>"
    else:
        positions_table = "<p><em>none</em></p>"
    bans = state.active_bans(today)
    bans_table = "<p><em>none</em></p>" if not bans else "<table><tr><th>symbol</th><th>reason</th></tr>" + "".join(
        f"<tr><td>{k}</td><td>{v}</td></tr>" for k, v in bans.items()
    ) + "</table>"
    # hypotheses / theses / breaker
    try:
        hyps = state.recent_hypotheses(limit=10)
        n_hyp = len(state.recent_hypotheses(limit=500))
    except Exception:
        hyps, n_hyp = [], 0
    try:
        n_thesis = state.conn.execute("SELECT COUNT(*) FROM theses").fetchone()[0]
        thesis_rows = state.conn.execute("SELECT * FROM theses ORDER BY created_at DESC LIMIT 5").fetchall()
    except Exception:
        n_thesis, thesis_rows = 0, []
    try:
        breaker_recs = [r for r in journal.tail(100) if r["type"] == "BREAKER_KILL"][-10:]
        n_breaker = len([r for r in journal.all_entries() if r["type"] == "BREAKER_KILL"])
    except Exception:
        breaker_recs, n_breaker = [], 0

    if hyps:
        hyp_table = "<table><tr><th>symbol</th><th>kind</th><th>outcome</th><th>pnl</th><th>lesson</th></tr>" + "".join(
            f"<tr><td>{r['symbol']}</td><td>{r['kind']}</td><td>{r['outcome']}</td><td>{r['pnl']}</td><td>{(r['lesson'] or '')[:80]}</td></tr>" for r in hyps
        ) + "</table>"
    else:
        hyp_table = "<p><em>none yet — negative results will appear here and feed next cycle's swarm</em></p>"
    theses_table = "<p><em>none yet</em></p>" if not thesis_rows else "<table><tr><th>position</th><th>thesis</th></tr>" + "".join(
        f"<tr><td>{r['position_id']}</td><td>{r['thesis_json'][:160]}</td></tr>" for r in thesis_rows
    ) + "</table>"
    breaker_tail = "\n".join(json.dumps(r, default=str) for r in breaker_recs) if breaker_recs else "(none)"

    tail = journal.tail(30)
    journal_tail = "\n".join(json.dumps(r, default=str) for r in tail) if tail else "(empty)"
    equity = "\n".join(f"{d}: {v:.2f}" for d, v in state.equity_history()[-30:]) or "(no data yet)"
    html = _HTML.format(
        today=today.isoformat(), halt=halt_txt, halt_cls=halt_cls,
        entries=state.entries_today(today), n_open=len(positions),
        agg=state.aggregate_open_risk(), positions_table=positions_table,
        bans_table=bans_table, journal_tail=journal_tail, equity=equity,
        n_hyp=n_hyp, n_thesis=n_thesis, n_breaker=n_breaker,
        hyp_table=hyp_table, theses_table=theses_table, breaker_tail=breaker_tail,
    )
    out.write_text(html)
    return out
