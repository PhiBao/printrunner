"""Supabase sink — mirrors journal / equity / hypotheses to Supabase so the
dashboard can be live without git commits. Fail-open: Supabase errors never
block trading; they are journaled and ignored.
"""
from __future__ import annotations

import os
import httpx

def _env() -> tuple[str | None, str | None]:
    url = os.getenv("SUPABASE_URL") or os.getenv("SUPABASE_PROJECT_URL")
    key = os.getenv("SUPABASE_SERVICE_KEY") or os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_ANON_KEY")
    return url, key

def push_journal(entry: dict) -> None:
    url, key = _env()
    if not url or not key:
        return
    try:
        # Supabase PostgREST expects /rest/v1/<table>
        endpoint = url.rstrip("/") + "/rest/v1/journal"
        # Map entry fields to table columns; payload is jsonb
        payload = {
            "seq": entry.get("seq"),
            "ts": entry.get("ts"),
            "entry_type": entry.get("type"),
            "cycle_id": entry.get("cycle_id"),
            "payload": entry.get("payload"),
            "prev_hash": entry.get("prev_hash"),
            "hash": entry.get("hash"),
        }
        httpx.post(
            endpoint,
            headers={
                "apikey": key,
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "Prefer": "return=minimal",
            },
            json=payload,
            timeout=5.0,
        )
    except Exception:
        pass  # fail-open

def push_equity(day: str, equity: float) -> None:
    url, key = _env()
    if not url or not key:
        return
    try:
        endpoint = url.rstrip("/") + "/rest/v1/equity"
        httpx.post(
            endpoint,
            headers={"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json", "Prefer": "resolution=merge-duplicates"},
            json={"day": day, "equity": equity, "updated_at": day},
            timeout=5.0,
        )
    except Exception:
        pass
