"""LLM team: Groq -> AIML -> OpenAI-compatible fallback.

Calls are JSON-forced, schema-validated, and guarded against hallucination:
a decision with an unknown candidate_id or conviction outside 1..5 is discarded
and the event is skipped (P1). Cached per shortlist hash so a retry/restore
never re-bills (confirm_cycles in calendar also prevents re-asking).
"""

from __future__ import annotations

import json
import re
from datetime import date, timedelta

import httpx

from ..config import Settings
from ..domain import CandidateBrief, Decision, stable_hash
from ..state.state import StateDB
from ..journal.journal import Journal


SYSTEM_PROMPT = (
    "You are the decision desk for PrintRunner, an earnings-season options agent. "
    "You ONLY pick from the provided candidate shortlist. You must output strictly "
    "as JSON per the schema. If no candidate is clearly better than doing nothing, "
    "return action=DECLINE_ALL. Never invent symbols, prices, or strikes. "
    "Rationale must be one short paragraph citing only the provided facts."
)

DECISION_SCHEMA_HINT = (
    'Respond as JSON: {"candidate_id": "<id or null>", "action": "SELECT|DECLINE_ALL", '
    '"conviction": 1-5, "rationale": "...", "considered": ["id", ...]} '
    "Rules: candidate_id must be one of the provided ids when action=SELECT; "
    "conviction 1-5; DECLINE_ALL ignores candidate_id."
)


def _provider_chain(settings: Settings) -> list[tuple[str, str, str, str]]:
    chain: list[tuple[str, str, str, str]] = []
    if settings.groq_key:
        chain.append(("groq", "https://api.groq.com/openai/v1/chat/completions",
                      settings.groq_key, settings.groq_model))
    if settings.aiml_key:
        chain.append(("aiml", "https://api.aimlapi.com/v1/chat/completions",
                      settings.aiml_key, settings.aiml_model))
    if settings.oai_compat_base_url and settings.oai_compat_key and settings.oai_compat_model:
        base = settings.oai_compat_base_url.rstrip("/")
        chain.append(("oai_compat", f"{base}/chat/completions",
                      settings.oai_compat_key, settings.oai_compat_model))
    return chain


def _call_provider(base_url: str, api_key: str, model: str, messages: list[dict]) -> str:
    resp = httpx.post(
        base_url,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": model,
            "messages": messages,
            "temperature": 0,
            "response_format": {"type": "json_object"},
        },
        timeout=30.0,
    )
    resp.raise_for_status()
    data = resp.json()
    return data["choices"][0]["message"]["content"]


def _strip_code_fences(text: str) -> str:
    m = re.search(r"```(?:json)?\s*(.*?)```", text, flags=re.DOTALL | re.IGNORECASE)
    return m.group(1).strip() if m else text.strip()


def _validate_decision(raw: dict, allowed_ids: set[str]) -> Decision | None:
    try:
        d = Decision.model_validate(raw)
    except Exception:
        return None
    if d.action == "SELECT":
        if d.candidate_id not in allowed_ids:
            return None
        if not (1 <= d.conviction <= 5):
            return None
        if len(d.rationale.strip()) < 20:
            return None
    if d.action == "DECLINE_ALL" and d.candidate_id not in (None,):
        # tolerate, but clear it
        d.candidate_id = None
    return d


SPECIALISTS = {
    "earnings": "You are an earnings specialist. You see ONLY historical earnings moves and expected move. No spot price. Summarize in 2 bullets whether the setup is rich/cheap and why.",
    "flow": "You are an options flow specialist. You see ONLY option open interest and spreads per leg. No spot. Summarize in 2 bullets on liquidity and premium richness.",
    "technicals": "You are a technicals specialist. You see ONLY recent closes, drift, and HV. No spot. Summarize in 2 bullets on trend and vol regime.",
    "sentiment": "You are a sentiment specialist. You see ONLY headlines. Summarize in 2 bullets.",
    "macro": "You are a macro specialist. You see ONLY SPY returns, VRP, and IV rank. Summarize in 2 bullets on market regime.",
}


class LLMTeam:
    def __init__(self, settings: Settings, state: StateDB, journal: Journal) -> None:
        self.settings = settings
        self.state = state
        self.journal = journal

    def _run_swarm(self, snapshot=None, metrics=None, briefs=None, headlines=None, cycle_id="") -> str:
        """Parallel specialists with no cross-talk, price hidden — post's pattern."""
        slices = {}
        if snapshot and metrics:
            slices["earnings"] = f"hist_moves={snapshot.hist_earn_moves} em_pct={metrics.expected_move_pct:.3f} move_ratio={metrics.move_ratio:.2f}"
            slices["flow"] = "; ".join(f"{q.option_symbol} OI={q.open_interest} spread={(q.ask-q.bid)/q.mid:.1%}" for q in snapshot.chain[:6]) or "no chain"
            slices["technicals"] = f"closes={snapshot.closes_recent} drift={metrics.runup_drift:.2%} hv20={snapshot.hv20}"
            slices["sentiment"] = "; ".join(headlines[:6]) if headlines else "no headlines"
            slices["macro"] = f"spy1d={metrics.spy_ret_1d} spy5d={metrics.spy_ret_5d} vrp={metrics.vrp} iv_rank={metrics.iv_rank}"
        else:
            return ""
        parts: list[str] = []
        for name, sys in SPECIALISTS.items():
            user = slices.get(name, "")
            for prov_name, url, key, model in _provider_chain(self.settings):
                try:
                    out = _call_provider(url, key, model, [{"role": "system", "content": sys}, {"role": "user", "content": user}])
                    parts.append(f"{name}: {_strip_code_fences(out)[:300]}")
                    break
                except Exception:
                    continue
        if parts:
            self.journal.append("LLM_CALL", {"swarm": True, "parts": len(parts)}, cycle_id)
        return "\n".join(parts)

    def decide(
        self,
        event_id: str,
        briefs: list[CandidateBrief],
        headlines: list[str],
        today: date,
        cycle_id: str,
        snapshot=None,
        metrics=None,
    ) -> Decision:
        if not briefs:
            return Decision(action="DECLINE_ALL", conviction=1, rationale="no candidates")
        allowed = {b.candidate_id for b in briefs}
        cache_key = stable_hash({"event": event_id, "briefs": [b.model_dump() for b in briefs], "day": today.isoformat()})
        cached = self.state.llm_cache_get(cache_key)
        if cached:
            dec = _validate_decision(cached, allowed)
            if dec:
                self.journal.append("LLM_CALL", {"provider": "cache", "event_id": event_id}, cycle_id)
                return dec

        # swarm + hypothesis graph (post's 6th piece)
        swarm_block = ""
        if snapshot is not None and metrics is not None:
            try:
                swarm_block = self._run_swarm(snapshot, metrics, briefs, headlines, cycle_id)
            except Exception:
                swarm_block = ""
        # hypothesis graph: inject 3 most similar past lessons
        hyp_block = ""
        if metrics is not None:
            try:
                regime = {"move_ratio": metrics.move_ratio, "vrp": metrics.vrp or 0, "drift": metrics.runup_drift, "spy5d": metrics.spy_ret_5d or 0}
                sims = self.state.similar_hypotheses(regime, symbol=event_id.split(":")[0], limit=3)
                if sims:
                    hyp_block = "Similar past hypotheses (most valuable are failures):\n" + "\n".join(
                        f"- {r['kind']} {r['outcome']} pnl={r['pnl']} lesson={r['lesson'] or ''} regime={r['regime_json'][:120]}" for r in sims
                    )
            except Exception:
                pass

        prompt_briefs = "\n".join(
            f"- {b.candidate_id}: {b.kind} {b.label} | max_loss {b.max_loss_per_contract:.0f} "
            f"max_profit {b.max_profit_per_contract:.0f} breakevens {b.breakevens} cost {b.entry_cost_per_contract:.2f}"
            for b in briefs
        )
        prompt_news = "; ".join(headlines[:6]) if headlines else "(no headlines available)"
        extra = ""
        if swarm_block:
            extra += f"\nSpecialist swarm (no price anchoring):\n{swarm_block}\n"
        if hyp_block:
            extra += f"\n{hyp_block}\n"
        user = (
            f"Event {event_id} on {today.isoformat()}.\n"
            f"Candidates:\n{prompt_briefs}\n"
            f"Recent headlines: {prompt_news}\n"
            f"{extra}"
            f"{DECISION_SCHEMA_HINT}"
        )
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ]

        for name, url, key, model in _provider_chain(self.settings):
            try:
                raw_text = _call_provider(url, key, model, messages)
                raw_text = _strip_code_fences(raw_text)
                parsed = json.loads(raw_text)
                dec = _validate_decision(parsed, allowed)
                if dec is None:
                    self.journal.append("DECISION_REJECTED", {"provider": name, "raw": raw_text[:800], "reason": "schema/hallucination guard"}, cycle_id)
                    continue
                self.state.llm_cache_put(cache_key, name, model, dec.model_dump())
                self.journal.append("LLM_CALL", {"provider": name, "model": model, "event_id": event_id, "decision": dec.model_dump()}, cycle_id)
                return dec
            except Exception as exc:
                self.journal.append("LLM_CALL", {"provider": name, "error": str(exc)[:600]}, cycle_id)
                continue

        self.journal.append("DECISION_REJECTED", {"event_id": event_id, "reason": "all_providers_failed"}, cycle_id)
        return Decision(action="DECLINE_ALL", conviction=1, rationale="providers failed — fail closed")

    def reviewer_bans(self, today: date, cycle_id: str) -> None:
        """LLM reviewer that can ONLY add bans (P6). Deterministic winners also banned."""
        # deterministic: winners banned for 2 days (no re-entry on a heater)
        for pos in self.state.all_positions():
            if pos.status.value == "CLOSED" and pos.realized_pnl is not None and pos.realized_pnl > 0:
                try:
                    close_day = pos.closed_at.date() if pos.closed_at else today  # type: ignore[union-attr]
                    if (today - close_day).days <= 2:
                        self.state.add_ban(pos.symbol, "winner cooldown", today + timedelta(days=2))
                        self.journal.append("REVIEWER_BAN", {"symbol": pos.symbol, "reason": "winner cooldown"}, cycle_id)
                except Exception:
                    continue
        if not self.settings.has_llm_provider():
            return
        # LLM reviewer: ask which symbols look over-traded this week (still only adds bans)
        # keep prompt tiny and JSON-forced; if it returns non-universe symbols, discard.
        try:
            import yaml

            universe = self.settings.load_universe()
        except Exception:
            universe = []
        if not universe:
            return
        # build a minimal summary for the reviewer
        recent = [p for p in self.state.all_positions() if p.opened_at and p.opened_at.date() >= today]  # type: ignore[union-attr]
        summary = "; ".join(f"{p.symbol}:{p.status.value}" for p in recent[:12]) or "no opens today"
        bans_now = self.state.active_bans(today)
        messages = [
            {"role": "system", "content": "You are a risk reviewer. You may ONLY suggest bans. Reply as JSON {\"bans\": [{\"symbol\": \"AAPL\", \"reason\": \"...\"}]} using only provided universe symbols. No other output."},
            {"role": "user", "content": f"Universe: {', '.join(universe[:60])}\nToday {today.isoformat()} recent: {summary}\nActive bans: {list(bans_now)}\nReturn JSON with 0-3 bans to add for 2 days."},
        ]
        for name, url, key, model in _provider_chain(self.settings):
            try:
                raw = _strip_code_fences(_call_provider(url, key, model, messages))
                data = json.loads(raw)
                for item in (data.get("bans") or [])[:3]:
                    sym = str(item.get("symbol", "")).upper()
                    reason = str(item.get("reason", ""))[:120]
                    if sym not in universe:
                        continue
                    self.state.add_ban(sym, reason or "reviewer", today + timedelta(days=2))
                    self.journal.append("REVIEWER_BAN", {"symbol": sym, "reason": reason, "provider": name}, cycle_id)
                break
            except Exception:
                continue
