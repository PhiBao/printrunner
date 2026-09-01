"""Order construction + fail-closed broker wrapper.

Every order gets a deterministic client_order_id so a retry/restore never
duplicates a position (P4). Failures are journaled and never retried blind.
"""

from __future__ import annotations

from datetime import datetime, timezone

import httpx

from ..config import Settings
from ..domain import Structure, stable_hash
from ..journal.journal import Journal
from ..state.state import StateDB


def client_order_id(structure: Structure, event_id: str, cycle_date: str) -> str:
    """Deterministic, side-free id — the broker rejects duplicates."""
    payload = {"event": event_id, "structure": structure.structure_id, "cycle": cycle_date}
    return f"pr-{stable_hash(payload)}"


def signed_limit(structure: Structure, price_per_share: float) -> float:
    """Alpaca mleg convention: credit spreads carry a negative limit price."""
    p = abs(price_per_share)
    return -p if structure.entry_cost_per_contract < 0 else p


def build_mleg_payload(structure: Structure, qty: int, limit_price: float,
                       client_oid: str) -> dict:
    legs = []
    for leg in structure.legs:
        legs.append({
            "symbol": leg.option_symbol,
            "ratio_qty": leg.ratio,
            "side": leg.side,
        })
    return {
        "order_class": "mleg",
        "type": "limit",
        "time_in_force": "day",
        "qty": qty,
        "limit_price": str(round(float(limit_price), 4)),
        "client_order_id": client_oid,
        "legs": legs,
    }


class AlpacaBroker:
    """Thin, testable wrapper around Alpaca Trading API order endpoints.

    In tests a FakeBroker duck-typing the same methods can be injected.
    In production this uses the same TradingClient credentials as the
    market-data service but hits the orders subdomain, adding auth headers.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._base = settings.alpaca_base_url.rstrip("/")
        self._headers = {
            "APCA-API-KEY-ID": settings.alpaca_key_id,
            "APCA-API-SECRET-KEY": settings.alpaca_secret,
            "Content-Type": "application/json",
        }

    def submit(self, payload: dict) -> dict:
        url = f"{self._base}/v2/orders"
        resp = httpx.post(url, json=payload, headers=self._headers, timeout=20.0)
        resp.raise_for_status()
        return resp.json()

    def orders(self, status: str = "open") -> list[dict]:
        """List orders with a status filter. Portable fallback: fetch all, filter locally."""
        url = f"{self._base}/v2/orders"
        resp = httpx.get(url, headers=self._headers, params={"status": status, "limit": 500},
                         timeout=15.0)
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else []

    def order_by_client_id(self, client_oid: str) -> dict | None:
        """Fetch one order by client_order_id (404 -> None)."""
        url = f"{self._base}/v2/orders:by_client_order_id"
        resp = httpx.get(url, headers=self._headers,
                         params={"client_order_id": client_oid}, timeout=10.0)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()

    def positions(self) -> list[dict]:
        url = f"{self._base}/v2/positions"
        resp = httpx.get(url, headers=self._headers, timeout=10.0)
        resp.raise_for_status()
        return resp.json() if isinstance(resp.json(), list) else []

    def cancel_order(self, order_id: str) -> None:
        httpx.delete(f"{self._base}/v2/orders/{order_id}", headers=self._headers, timeout=10.0)


def submit_entry(
    structure: Structure,
    event_id: str,
    cycle_date: str,
    contracts: int,
    limit_price: float,
    cycle_id: str,
    journal: Journal,
    state: StateDB,
    broker: AlpacaBroker,
    position_id: str,
) -> dict | None:
    """Fail-closed entry submission. Returns broker order on success, None on failure."""
    cid = client_order_id(structure, event_id, cycle_date)
    # P4: deterministic id collision check
    existing = state.orders_for_position(position_id)
    for row in existing:
        if row["client_order_id"] == cid:
            journal.append("ORDER_REJECTED", {"position_id": position_id, "reason": "order_already_exists", "client_order_id": cid}, cycle_id)
            return None
    try:
        broker_existing = broker.order_by_client_id(cid)
        if broker_existing is not None:
            journal.append("ORDER_REJECTED", {"reason": "broker_already_has_order", "client_order_id": cid}, cycle_id)
            return None
    except Exception:
        pass  # best-effort; submit will dedup anyway via broker 409

    payload = build_mleg_payload(structure, contracts, limit_price, cid)
    try:
        order = broker.submit(payload)
        state.add_order(cid, position_id, "ENTRY", order.get("id"))
        journal.append("ORDER_SUBMITTED", {"position_id": position_id, "client_order_id": cid, "broker_order_id": order.get("id"), "limit": limit_price}, cycle_id)
        return order
    except httpx.HTTPStatusError as exc:
        body = ""
        try:
            body = exc.response.text[:600]
        except Exception:
            pass
        journal.append("ORDER_REJECTED", {"position_id": position_id, "error": str(exc), "body": body}, cycle_id)
        state.add_order(cid, position_id, "ENTRY")
        state.update_order_status(cid, "REJECTED")
        return None
    except Exception as exc:
        journal.append("ORDER_REJECTED", {"position_id": position_id, "error": str(exc)}, cycle_id)
        state.add_order(cid, position_id, "ENTRY")
        state.update_order_status(cid, "REJECTED")
        return None
