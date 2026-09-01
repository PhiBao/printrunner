"""Autopsy — reads closed position + its thesis, finds where it bled, writes lesson.
Post's post-mortem + fine-tune: every decision gets logged with reasoning trace,
critic surfaces pattern, thesis preregistered then compared.
"""

from __future__ import annotations

import json
from datetime import date

from ..journal.journal import Journal
from ..state.state import StateDB


def run_autopsy(state: StateDB, journal: Journal, today: date, cycle_id: str) -> int:
    """For each CLOSED position with a PENDING hypothesis, compare thesis vs reality."""
    n = 0
    for pos in state.all_positions():
        if pos.status.value != "CLOSED":
            continue
        # find matching hypothesis still pending
        for row in state.recent_hypotheses(symbol=pos.symbol, limit=20):
            if row["event_id"] != pos.event_id or row["outcome"] != "PENDING":
                continue
            thesis_row = state.get_thesis(pos.position_id)
            thesis = json.loads(thesis_row["thesis_json"]) if thesis_row else {}
            expected = thesis.get("expected_pnl_pct", 0)
            actual = (pos.realized_pnl or 0) / (pos.structure.max_loss_per_contract * pos.structure.contracts) if pos.structure.max_loss_per_contract else 0
            bled = "thesis met" if (pos.realized_pnl or 0) > 0 else "bled"
            # naive lesson: where expected vs actual diverged
            lesson = f"{bled}: expected {expected:.0%}, actual {actual:.1%}, reason={pos.exit_reason or 'unknown'}, thesis_hash={thesis.get('thesis_hash','')[:8] if isinstance(thesis, dict) else ''}"
            state.update_hypothesis(row["id"], "CLOSED", pnl=pos.realized_pnl, lesson=lesson)
            journal.append("AUTOPSY", {"position_id": pos.position_id, "thesis": thesis, "actual_pnl": pos.realized_pnl, "lesson": lesson}, cycle_id)
            n += 1
            break
    return n
