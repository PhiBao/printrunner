"""PrintRunner — earnings-season options agent for Alpaca paper trading.

Design laws (see ARCHITECTURE.md):
  P1 LLM is the least-trusted component — it selects from a validated shortlist.
  P2 Defined-risk only, structurally — every position is one mleg order, max loss known.
  P3 Fail closed — uncertainty means "don't trade", never "try anyway".
  P4 Restart can't hurt — deterministic client_order_ids + reconcile-before-write.
  P5 Point-in-time honest — calendar provenance and reschedule detection.
  P6 Adapt only toward restriction — the reviewer can ban, never loosen.
  P7 Paper or nothing — boot guard refuses live credentials.
  P8 The agent earns trust by saying NO — every rejection is journaled.
"""

__version__ = "0.1.0"
