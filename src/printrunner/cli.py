"""CLI: `pr` — cycle, status, journal, resume, dashboard, verify."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import Settings
from .journal.journal import Journal
from .state.state import StateDB
from .util import today_et


def cmd_status(args) -> int:
    settings = Settings.load()
    state = StateDB(settings.db_path)
    journal = Journal(settings.journal_path)
    try:
        journal.verify()
        jstat = "ok"
    except Exception as exc:
        jstat = f"BROKEN: {exc}"
    halt = state.halt()
    print(f"halt: {halt.tripped} {halt.reason or ''}")
    print(f"journal: {jstat}  entries={len(journal.all_entries())}")
    print(f"open_positions: {len(state.open_positions())}")
    print(f"aggregate_risk: {state.aggregate_open_risk():.0f}")
    print(f"entries_today: {state.entries_today(today_et())}")
    print(f"active_bans: {state.active_bans(today_et())}")
    return 0


def cmd_journal(args) -> int:
    settings = Settings.load()
    j = Journal(settings.journal_path)
    if args.verify:
        try:
            j.verify()
            print("chain ok")
            return 0
        except Exception as exc:
            print(f"chain broken: {exc}", file=sys.stderr)
            return 1
    for rec in j.tail(args.n):
        print(rec)
    return 0


def cmd_resume(args) -> int:
    settings = Settings.load()
    state = StateDB(settings.db_path)
    journal = Journal(settings.journal_path)
    if args.confirm != "ACK":
        print("refusing: pass --confirm ACK to clear the latched halt", file=sys.stderr)
        return 2
    halt = state.halt()
    if not halt.tripped:
        print("no halt is latched")
        return 0
    state.clear_halt()
    journal.append("RESUME", {"prev_reason": halt.reason}, None)
    print(f"cleared halt: {halt.reason}")
    return 0


def cmd_dashboard(args) -> int:
    settings = Settings.load()
    from .dashboard.build import build_dashboard

    out = build_dashboard(settings)
    print(f"dashboard written to {out}")
    return 0


def cmd_doctor(args) -> int:
    """Environment self-check: paper guard, journal chain, Alpaca CLI channel."""
    from .config import LiveEndpointError
    try:
        settings = Settings.load()
        print(f"paper guard: ok ({settings.alpaca_base_url})")
    except LiveEndpointError as exc:
        print(f"paper guard: REFUSED ({exc})", file=sys.stderr)
        return 2
    state = StateDB(settings.db_path)
    journal = Journal(settings.journal_path)
    try:
        journal.verify()
        print(f"journal: ok entries={len(journal.all_entries())}")
    except Exception as exc:
        print(f"journal: BROKEN ({exc})", file=sys.stderr)
    halt = state.halt()
    print(f"halt: {halt.tripped} {halt.reason or ''}")
    from .execution.alpaca_cli import probe
    info = probe(settings)
    if not info.get("available"):
        print("alpaca cli: not installed (REST only)")
        return 0
    print(f"alpaca cli: account_ok={info.get('account_ok')} "
          f"equity={info.get('equity')} positions={info.get('positions')} "
          f"clock_is_open={info.get('clock_is_open', info.get('clock_open'))}")
    if info.get("live_forced_off"):
        print("alpaca cli: ALPACA_LIVE_TRADE was set — forced off (paper only)")
    return 0 if info.get("account_ok") else 1


def cmd_cycle(args) -> int:
    settings = Settings.load()
    from .orchestrator.cycle import run_cycle

    summary = run_cycle(settings)
    print(summary)
    if summary.get("errors"):
        return 1
    return 0


def main() -> None:
    p = argparse.ArgumentParser(prog="pr")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    pj = sub.add_parser("journal")
    pj.add_argument("-n", type=int, default=20)
    pj.add_argument("--verify", action="store_true")
    pr = sub.add_parser("resume")
    pr.add_argument("--confirm", default="")
    sub.add_parser("dashboard")
    sub.add_parser("cycle")
    sub.add_parser("doctor")
    args = p.parse_args()
    code = {"status": cmd_status, "journal": cmd_journal, "resume": cmd_resume,
            "dashboard": cmd_dashboard, "cycle": cmd_cycle, "doctor": cmd_doctor}[args.cmd](args)
    sys.exit(code)
