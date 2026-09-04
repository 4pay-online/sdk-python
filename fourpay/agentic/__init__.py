"""Agentic commerce.

Four protocols, four different questions:

* **MCP** — how an agent reaches your tools (:mod:`fourpay.agentic.mcp`);
* **AP2** — how you prove a human authorised what the agent is buying
  (:mod:`fourpay.agentic.ap2`);
* **ACP** — how a checkout runs inside someone else's chat
  (:mod:`fourpay.agentic.acp`);
* **A2A** — how one agent hires another (:mod:`fourpay.agentic.a2a`).

They compose: an ACP checkout can carry an AP2 mandate, and an A2A task can be
what one agent sends another to start it.
"""

from . import a2a, acp, ap2, mcp  # noqa: F401
from .tools import execute_payment_tool, payment_tools  # noqa: F401

__all__ = ["a2a", "acp", "ap2", "mcp", "payment_tools", "execute_payment_tool"]
