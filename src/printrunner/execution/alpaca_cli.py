"""Alpaca CLI second channel — independent broker-truth reads (fail-open).

Uses the official alpacahq/cli binary when present for read-only broker
verification (account / positions / clock) alongside the REST path. Env
credentials are mapped from Settings at call time; secrets never touch disk
or logs, and live trading is never opted into (ALPACA_LIVE_TRADE is stripped
so the CLI stays on its paper default, matching P7).

Any failure — missing binary, non-zero exit, unparsable output — returns
None and the caller falls back to REST. The CLI can only add evidence to the
journal; it can never block trading (P3 direction: uncertainty in the
*verifier* must not stop the *executor's* fail-closed logic, nor invent
fills).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Any


def available() -> bool:
    """True when the `alpaca` binary is on PATH."""
    return shutil.which("alpaca") is not None


def _env(settings) -> tuple[dict[str, str], bool]:
    """Build a scrubbed env for the CLI. Returns (env, live_was_forced_off)."""
    env = dict(os.environ)
    env["ALPACA_API_KEY"] = settings.alpaca_key_id or ""
    env["ALPACA_SECRET_KEY"] = settings.alpaca_secret or ""
    env["ALPACA_QUIET"] = "1"
    forced_off = env.pop("ALPACA_LIVE_TRADE", None) == "true"
    return env, forced_off


def _run(argv: list[str], settings, timeout: float = 25.0) -> tuple[int, str, str]:
    """Run the CLI, never leaking secrets. Returns (returncode, stdout, stderr)."""
    env, _ = _env(settings)
    try:
        proc = subprocess.run(
            ["alpaca", *argv],
            capture_output=True, text=True, timeout=timeout, env=env,
        )
        return proc.returncode, (proc.stdout or ""), (proc.stderr or "")[-500:]
    except FileNotFoundError:
        return 127, "", "alpaca binary not found"
    except subprocess.TimeoutExpired:
        return 124, "", "alpaca cli timeout"
    except Exception as exc:  # pragma: no cover — defensive
        return 1, "", f"alpaca cli launch failed: {type(exc).__name__}"


def _parse_json(stdout: str) -> Any | None:
    try:
        text = stdout.strip()
        if not text:
            return None
        return json.loads(text)
    except Exception:
        return None


def cli_account(settings) -> dict | None:
    """Account snapshot via CLI, or None on any failure."""
    if not available():
        return None
    code, out, _ = _run(["account", "get", "--quiet"], settings)
    if code != 0:
        return None
    data = _parse_json(out)
    return data if isinstance(data, dict) else None


def cli_positions(settings) -> list | None:
    """Open positions via CLI, or None on any failure."""
    if not available():
        return None
    code, out, _ = _run(["position", "list", "--quiet"], settings)
    if code != 0:
        return None
    data = _parse_json(out)
    return data if isinstance(data, list) else None


def cli_clock(settings) -> dict | None:
    """Market clock via CLI, or None on any failure."""
    if not available():
        return None
    code, out, _ = _run(["clock", "--quiet"], settings)
    if code != 0:
        return None
    data = _parse_json(out)
    return data if isinstance(data, dict) else None


def probe(settings) -> dict:
    """Machine-readable self-check for `pr doctor` and the journal.

    Never includes secrets — only shapes/booleans and truncated errors.
    """
    if not available():
        return {"available": False}
    out: dict = {"available": True}
    _, forced_off = _env(settings)
    if forced_off:
        out["live_forced_off"] = True
    acct = cli_account(settings)
    out["account_ok"] = bool(acct)
    if acct:
        for key in ("equity", "buying_power", "cash", "status"):
            if key in acct:
                out[key] = acct[key]
    pos = cli_positions(settings)
    out["positions"] = len(pos) if isinstance(pos, list) else None
    clock = cli_clock(settings)
    if isinstance(clock, dict):
        for key in ("is_open", "is_open_now", "open", "next_open", "next_close", "timestamp"):
            if key in clock:
                out[f"clock_{key}"] = clock[key]
    return out
