import tempfile
from pathlib import Path
from datetime import date, datetime, timedelta, timezone

from printrunner.config import RiskParams
from printrunner.domain import EarningsEvent, MarketDataSnapshot, OptionQuote, StockQuote, utcnow
from printrunner.marketdata.bs import bs_price, bs_delta, implied_vol, straddle_iv
from printrunner.screener.metrics import select_expiry, compute_metrics
from printrunner.screener.desk import build_structures
from printrunner.risk.gates import evaluate_gates
from printrunner.journal.journal import Journal
from printrunner.state.state import StateDB
from printrunner.execution.broker import client_order_id, signed_limit
from printrunner.positions.exits import build_exit_plan, evaluate_exit
from printrunner.llm.team import _validate_decision
from printrunner.marketdata.alpaca import AccountInfo


def _quote(sym, strike, expiry, mid, *, otype="call", oi=500, bid_off=0.05):
    q = OptionQuote(
        option_symbol=sym, expiry=expiry, strike=strike, option_type=otype,  # type: ignore[arg-type]
        bid=round(mid - bid_off, 2), ask=round(mid + bid_off, 2), open_interest=oi,
        quoted_at=utcnow(),
    )
    return q


def _chain_for_expiry(spot, expiry, strikes, call_mids, put_mids):
    out = []
    for k, m in zip(strikes, call_mids):
        out.append(_quote(f"C{k}", k, expiry, m, otype="call"))
    for k, m in zip(strikes, put_mids):
        out.append(_quote(f"P{k}", k, expiry, m, otype="put"))
    return out


def _now():
    return utcnow()


def test_bs_roundtrip():
    S, K, T, sigma = 100, 100, 30/365, 0.3
    price = bs_price(S, K, T, sigma, True)
    iv = implied_vol(price, S, K, T, True)
    assert iv is not None and abs(iv - sigma) < 0.02


def test_bs_delta_bounds():
    d = bs_delta(100, 100, 30/365, 0.3, True)
    assert 0.45 < d < 0.65
    d2 = bs_delta(100, 110, 30/365, 0.3, True)
    assert d2 < d


def test_straddle_iv():
    S, K, T = 100, 100, 14/365
    iv = 0.45
    call = bs_price(S, K, T, iv, True)
    put = bs_price(S, K, T, iv, False)
    solved = straddle_iv(call, put, S, K, T)
    assert solved is not None and abs(solved - iv) < 0.03


def test_select_expiry_prefers_7d():
    event = date(2026, 9, 10)
    expiries = [date(2026, 9, 12), date(2026, 9, 17), date(2026, 9, 19), date(2026, 9, 26)]
    # only [14,17,19] in [event+4, event+10] = [14,20]
    chosen = select_expiry(expiries, event)
    assert chosen == date(2026, 9, 17)


def test_metrics_em_and_move_ratio():
    today = date(2026, 9, 1)
    event = EarningsEvent(symbol="AAPL", event_date=date(2026, 9, 10), source="finnhub", captured_at=_now())
    spot = 200.0
    expiry = date(2026, 9, 17)
    chain = _chain_for_expiry(spot, expiry, [195, 200, 205], [2.0, 3.0, 1.2], [1.1, 2.8, 0.9])
    # ATM 200 straddle = 5.8 => em_pct 2.9%
    snap = MarketDataSnapshot(
        symbol="AAPL", fetched_at=_now(), spot=spot,
        quote=StockQuote(bid=199.5, ask=200.5, quoted_at=_now()),
        chain=chain, spy_spot=500, spy_quote=StockQuote(quoted_at=_now()),
        hist_earn_moves=[0.02, 0.03, 0.015, 0.025, 0.018, 0.022],
        closes_recent=[195, 196, 197, 198, 199, 200],
        hv20=0.22,
    )
    metrics, failures, exp = compute_metrics(snap, event, today, RiskParams())
    assert failures == []
    assert exp == expiry
    assert abs(metrics.expected_move_pct - 0.029) < 0.002  # type: ignore[union-attr]
    assert metrics.move_ratio > 1.0  # type: ignore[union-attr]


def test_metrics_insufficient_history_fails_g1():
    today = date(2026, 9, 1)
    event = EarningsEvent(symbol="AAPL", event_date=date(2026, 9, 10), source="finnhub", captured_at=_now())
    expiry = date(2026, 9, 17)
    chain = _chain_for_expiry(200.0, expiry, [200], [3.0], [2.8])
    snap = MarketDataSnapshot(
        symbol="AAPL", fetched_at=_now(), spot=200.0,
        quote=StockQuote(quoted_at=_now()),
        chain=chain, spy_spot=500, spy_quote=StockQuote(quoted_at=_now()),
        hist_earn_moves=[0.02], closes_recent=[200]*6,
    )
    metrics, failures, _ = compute_metrics(snap, event, today, RiskParams())
    assert metrics is None and any(c.value == "G1_missing_data" for c, _ in failures)


def test_desk_builds_verticals():
    today = date(2026, 9, 1)
    event = EarningsEvent(symbol="AAPL", event_date=date(2026, 9, 10), source="finnhub", captured_at=_now())
    spot = 200.0
    expiry = date(2026, 9, 17)
    strikes = [190, 195, 200, 205, 210, 215]
    # craft mids that give sensible deltas: OTM cheaper
    call_mids = [8, 5, 3.0, 1.5, 0.7, 0.3]
    put_mids = [0.3, 0.8, 2.8, 5.0, 8.0, 12.0]
    chain = _chain_for_expiry(spot, expiry, strikes, call_mids, put_mids)
    snap = MarketDataSnapshot(
        symbol="AAPL", fetched_at=_now(), spot=spot,
        quote=StockQuote(quoted_at=_now()), chain=chain,
        spy_spot=500, spy_quote=StockQuote(quoted_at=_now()),
        hist_earn_moves=[0.03]*6, closes_recent=[195, 196, 197, 198, 199, 200], hv20=0.2,
    )
    metrics, _, _ = compute_metrics(snap, event, today, RiskParams())
    assert metrics is not None
    structs = build_structures(snap, metrics, event, today, RiskParams())
    kinds = {s.kind.value for s in structs}
    assert "call_debit_vertical" in kinds or "put_debit_vertical" in kinds


def test_gates_stale_and_liquidity():
    today = date(2026, 9, 1)
    now = _now()
    old = now - timedelta(minutes=40)
    chain = [_quote("C200", 200, date(2026, 9, 17), 3.0, oi=10, bid_off=0.5)]
    # force stale + low OI
    chain[0] = chain[0].model_copy(update={"quoted_at": old, "open_interest": 5})
    snap = MarketDataSnapshot(
        symbol="AAPL", fetched_at=now, spot=200.0,
        quote=StockQuote(bid=199, ask=201, quoted_at=old),
        chain=chain, spy_spot=500, spy_quote=StockQuote(quoted_at=now),
        hist_earn_moves=[0.02]*6, closes_recent=[195, 196, 197, 198, 199, 200], hv20=0.2,
    )
    # minimal metrics to exercise gates
    from printrunner.domain import Metrics, Structure, StructureKind, Leg, ExitPlan
    metrics = Metrics(expected_move_pct=0.03, expected_move_usd=6, move_ratio=1.5, hist_move_sample=6, runup_drift=0.02, vrp=0.05, spy_ret_1d=0, spy_ret_5d=0)
    struct = Structure(
        structure_id="test", kind=StructureKind.CALL_DEBIT_VERTICAL,
        legs=[Leg(option_symbol="C200", side="buy", quote_at_selection=chain[0]),
              Leg(option_symbol="C205", side="sell", quote_at_selection=_quote("C205", 205, date(2026,9,17), 1.5))],
        expires_on=date(2026, 9, 17), max_loss_per_contract=150, max_profit_per_contract=350,
        breakevens=[201.5], entry_cost_per_contract=1.5, label="test",
    )
    with tempfile.TemporaryDirectory() as td:
        state = StateDB(Path(td) / "db.sqlite")
        out = evaluate_gates(struct, snap, metrics, today, now, RiskParams(), state, AccountInfo(equity=100000, buying_power=50000, cash=50000), conviction=5)
        assert not out.passed
        assert any(c.value in ("G2_stale_quotes", "G6_liquidity") for c, _ in out.failures)


def test_client_order_id_deterministic():
    from printrunner.domain import Structure, StructureKind, Leg
    q = _quote("C200", 200, date(2026, 9, 17), 3.0)
    s = Structure(structure_id="abc", kind=StructureKind.CALL_DEBIT_VERTICAL, legs=[Leg(option_symbol="C200", side="buy", quote_at_selection=q)], expires_on=date(2026,9,17), max_loss_per_contract=100, max_profit_per_contract=100, breakevens=[201], entry_cost_per_contract=1, label="x")
    a = client_order_id(s, "AAPL:2026-09-10", "2026-09-01")
    b = client_order_id(s, "AAPL:2026-09-10", "2026-09-01")
    assert a == b and a.startswith("pr-")


def test_signed_limit():
    from printrunner.domain import Structure, StructureKind, Leg
    q = _quote("C200", 200, date(2026, 9, 17), 1.0)
    condor = Structure(structure_id="c", kind=StructureKind.IRON_CONDOR, legs=[Leg(option_symbol="x", side="buy", quote_at_selection=q)], expires_on=date(2026,9,17), max_loss_per_contract=100, max_profit_per_contract=100, breakevens=[1], entry_cost_per_contract=-1.2, label="x")
    assert signed_limit(condor, 1.2) < 0
    vert = condor.model_copy(update={"kind": StructureKind.CALL_DEBIT_VERTICAL, "entry_cost_per_contract": 1.2})
    assert signed_limit(vert, 1.2) > 0


def test_exit_plan_and_eval():
    from printrunner.domain import Position, PositionStatus, Structure, StructureKind, Leg

    q_long = _quote("C200", 200, date(2026, 9, 17), 3.0)
    q_short = _quote("C205", 205, date(2026, 9, 17), 1.5)
    struct = Structure(structure_id="s", kind=StructureKind.CALL_DEBIT_VERTICAL,
                       legs=[Leg(option_symbol="C200", side="buy", quote_at_selection=q_long),
                             Leg(option_symbol="C205", side="sell", quote_at_selection=q_short)],
                       expires_on=date(2026, 9, 20), max_loss_per_contract=150,
                       max_profit_per_contract=350, breakevens=[201.5], entry_cost_per_contract=1.5, label="x")
    plan = build_exit_plan(struct.kind, RiskParams(), debit_per_share=1.5, width_per_share=5)
    pos = Position(position_id="p", event_id="AAPL:2026-09-10", symbol="AAPL", structure=struct,
                   entry_order_client_id="cid", exit_plan=plan, opened_at=_now())
    # target: value >= 1.5*1.4=2.1 ; provide quotes giving value 2.5
    quotes = {"C200": _quote("C200", 200, date(2026, 9, 20), 3.5), "C205": _quote("C205", 205, date(2026, 9, 20), 1.0)}
    action, _ = evaluate_exit(pos, quotes, spot=202, today=date(2026, 9, 5), risk=RiskParams())
    assert action == "CLOSE"


def test_llm_hallucination_guard():
    allowed = {"a", "b"}
    assert _validate_decision({"action": "SELECT", "candidate_id": "c", "conviction": 5, "rationale": "x"*30}, allowed) is None
    assert _validate_decision({"action": "SELECT", "candidate_id": "a", "conviction": 5, "rationale": "x"*30}, allowed) is not None
    assert _validate_decision({"action": "DECLINE_ALL", "conviction": 2, "rationale": "nope"}, allowed) is not None


def test_journal_chain_and_tamper(tmp_path=None):
    p = Path(tempfile.mkdtemp()) / "j.jsonl"
    j = Journal(p)
    j.append("BOOT", {"x": 1}, "c1")
    j.append("SCREEN_PASS", {"s": "AAPL"}, "c1")
    assert j.verify() is True
    # tamper
    lines = p.read_text().splitlines()
    import json
    rec = json.loads(lines[0])
    rec["payload"]["x"] = 999
    p.write_text(json.dumps(rec) + "\n" + lines[1] + "\n")
    try:
        Journal(p)
        assert False, "should have raised"
    except RuntimeError as e:
        assert "broken" in str(e).lower()


def test_state_halt_latch(tmp_path=None):
    with tempfile.TemporaryDirectory() as td:
        db = StateDB(Path(td) / "db.sqlite")
        assert not db.halt().tripped
        db.latch_halt("test")
        assert db.halt().tripped
        db.clear_halt()
        assert not db.halt().tripped
