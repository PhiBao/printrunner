"""Unit tests for the Alpaca CLI second channel + calendar confirmation.

No network: subprocess and PATH lookup are monkeypatched; StateDB uses tmp.
"""

import json
import subprocess
import tempfile
from pathlib import Path
from datetime import date

from printrunner.execution import alpaca_cli as cli
from printrunner.state.state import StateDB


class _Done:
    def __init__(self, code=0, out="", err=""):
        self.returncode = code
        self.stdout = out
        self.stderr = err


def _settings():
    class S:
        alpaca_key_id = "key-id"
        alpaca_secret = "secret"
    return S()


def test_unavailable_without_binary(monkeypatch):
    monkeypatch.setattr(cli.shutil, "which", lambda *_: None)
    assert cli.available() is False
    assert cli.cli_account(_settings()) is None
    assert cli.cli_positions(_settings()) is None
    assert cli.probe(_settings()) == {"available": False}


def test_account_parses_equity(monkeypatch):
    monkeypatch.setattr(cli.shutil, "which", lambda *_: "/usr/bin/alpaca")
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"] = argv
        seen["env_keys"] = sorted(kw["env"])
        assert kw["env"]["ALPACA_API_KEY"] == "key-id"
        assert kw["env"]["ALPACA_SECRET_KEY"] == "secret"
        assert kw["env"]["ALPACA_QUIET"] == "1"
        assert "ALPACA_LIVE_TRADE" not in kw["env"]
        return _Done(0, json.dumps({"equity": "100000.5", "buying_power": "200000"}))

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    acct = cli.cli_account(_settings())
    assert acct and acct["equity"] == "100000.5"
    assert seen["argv"][:2] == ["alpaca", "account"]


def test_nonzero_exit_and_bad_json_are_none(monkeypatch):
    monkeypatch.setattr(cli.shutil, "which", lambda *_: "/usr/bin/alpaca")
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: _Done(1, "", "boom"))
    assert cli.cli_account(_settings()) is None
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: _Done(0, "not json"))
    assert cli.cli_positions(_settings()) is None


def test_live_trade_env_is_stripped(monkeypatch):
    monkeypatch.setattr(cli.shutil, "which", lambda *_: "/usr/bin/alpaca")
    monkeypatch.setenv("ALPACA_LIVE_TRADE", "true")
    seen = {}

    def fake_run(argv, **kw):
        seen["env"] = kw["env"]
        return _Done(0, "[]")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    assert cli.cli_positions(_settings()) == []
    assert "ALPACA_LIVE_TRADE" not in seen["env"]
    assert cli.probe(_settings()).get("live_forced_off") is True


def test_cli_never_raises_on_launch_failure(monkeypatch):
    monkeypatch.setattr(cli.shutil, "which", lambda *_: "/usr/bin/alpaca")

    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="alpaca", timeout=1)

    monkeypatch.setattr(cli.subprocess, "run", boom)
    assert cli.cli_clock(_settings()) is None


def test_calendar_confirm_increments_on_repeat_observation():
    with tempfile.TemporaryDirectory() as td:
        st = StateDB(Path(td) / "db.sqlite")
        r1 = st.upsert_calendar("ADBE", date(2026, 9, 10), "AMC", "finnhub")
        assert r1 == (None, 0)
        r2 = st.upsert_calendar("ADBE", date(2026, 9, 10), "AMC", "finnhub")
        assert r2 == (None, 1)
        # date change resets and reports reschedule
        r3 = st.upsert_calendar("ADBE", date(2026, 9, 11), "AMC", "finnhub")
        assert r3 == (date(2026, 9, 10), 0)


def test_waiver_parse_and_floor():
    from printrunner.domain import GateCode
    from printrunner.execution.waiver import WAIVABLE, parse_waived
    assert WAIVABLE == {GateCode.G9, GateCode.G10}
    assert parse_waived("G9,G10") == {GateCode.G9, GateCode.G10}
    assert parse_waived("g9_runup_favorable") == {GateCode.G9}
    assert parse_waived("") == set()
    try:
        parse_waived("G6")
        print("G6 parsed (allowed by parser, blocked by floor)")
    except ValueError:
        raise AssertionError("G6 is a known code and must parse")
    import pytest as _pt
    with _pt.raises(ValueError):
        parse_waived("G99")
