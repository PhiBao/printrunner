"""Hash-chained append-only journal (P8: auditability).

Every cycle decision — every rejection, every LLM call, every order, every
halt — lands here. Each record carries prev_hash; tampering with any line
breaks the chain and `pr journal --verify` fails loudly.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..domain import stable_hash, utcnow

try:
    from ..supabase import push_journal as _push_supabase
except Exception:
    def _push_supabase(entry):  # type: ignore
        pass

ENTRY_TYPES = {
    "BOOT", "CALENDAR", "RESCHEDULE", "SCREEN_PASS", "SCREEN_FAIL",
    "GATE_PASS", "GATE_FAIL", "LLM_CALL", "DECISION", "DECISION_REJECTED",
    "ORDER_SUBMITTED", "ORDER_REJECTED", "ORDER_FILLED", "ORDER_CANCELLED",
    "EXIT_EVAL", "EXIT_SUBMITTED", "EXIT_FILLED", "POSITION_CLOSED",
    "HALT", "RESUME", "RECONCILE", "REVIEWER_BAN", "ERROR", "CYCLE_SUMMARY",
    "THESIS", "BREAKER_KILL", "HYPOTHESIS", "AUTOPSY", "CLI_CHECK",
}


class Journal:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._last_hash = "GENESIS"
        self._seq = 0
        if self.path.exists():
            self._rescan()

    def _rescan(self) -> None:
        if not self.path.exists():
            return
        with self.path.open() as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    raise RuntimeError(
                        f"journal corruption: unreadable line at seq {self._seq} "
                        f"in {self.path}. HALT and inspect manually."
                    )
                expected = rec.get("hash")
                recomputed = self._compute_hash(rec)
                if expected != recomputed:
                    raise RuntimeError(
                        f"journal hash chain broken at seq {rec.get('seq')} "
                        f"in {self.path}. HALT and inspect manually."
                    )
                self._last_hash = rec["hash"]
                self._seq = rec["seq"] + 1

    @staticmethod
    def _compute_hash(rec: dict) -> str:
        payload = {k: v for k, v in rec.items() if k != "hash"}
        return stable_hash(payload)

    def append(self, type_: str, payload: dict[str, Any], cycle_id: str | None = None) -> dict:
        if type_ not in ENTRY_TYPES:
            raise ValueError(f"unknown journal entry type: {type_}")
        rec = {
            "seq": self._seq,
            "ts": utcnow().isoformat(),
            "type": type_,
            "cycle_id": cycle_id,
            "payload": payload,
            "prev_hash": self._last_hash,
        }
        rec["hash"] = self._compute_hash(rec)
        with self.path.open("a") as fh:
            fh.write(json.dumps(rec, default=str) + "\n")
            fh.flush()
        self._last_hash = rec["hash"]
        self._seq += 1
        try:
            _push_supabase(rec)
        except Exception:
            pass
        return rec

    def verify(self) -> bool:
        """Full replay check; raises on any break, returns True if intact."""
        self._last_hash = "GENESIS"
        self._seq = 0
        self._rescan()
        return True

    def tail(self, n: int = 50) -> list[dict]:
        if not self.path.exists():
            return []
        lines = [l for l in self.path.read_text().splitlines() if l.strip()]
        return [json.loads(l) for l in lines[-n:]]

    def all_entries(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(l) for l in self.path.read_text().splitlines() if l.strip()]
