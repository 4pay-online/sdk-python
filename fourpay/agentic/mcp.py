"""MCP — Model Context Protocol.

The platform ships an MCP server, in two builds because the two audiences are
not the same person:

=========================  ==========================  ===========================================
Server                     Who runs it                 What it exposes
=========================  ==========================  ===========================================
``4pay-payments-mcp``   the partner (merchant)      transactions, payment links, products, cards
``4pay-mcp``            the platform's client       the above plus terminals, routing, limits, FX
=========================  ==========================  ===========================================

A partner integrating payments wants the first. Handing them the operator build
gives an agent tools that can re-route live traffic, and no amount of prompting
reliably keeps a model out of a tool it can see.
"""

from __future__ import annotations

from typing import Any, Dict

__all__ = ["build_mcp_config"]


def build_mcp_config(
    api_key: str,
    organization_id: str,
    base_url: str = "https://4pay.online",
    scope: str = "payments",
    use_env_placeholders: bool = False,
) -> Dict[str, Any]:
    """Build the ``mcpServers`` entry for an MCP client.

    With ``use_env_placeholders`` the file carries ``${FOURPAY_API_KEY}`` rather
    than the key itself — worth doing, since these files end up in repositories.
    """
    package = "@4pay/payments-mcp" if scope == "payments" else "@4pay/mcp-server"
    env = (
        {
            "FOURPAY_API_URL": "${FOURPAY_API_URL}",
            "FOURPAY_API_KEY": "${FOURPAY_API_KEY}",
            "FOURPAY_ORGANIZATION_ID": "${FOURPAY_ORGANIZATION_ID}",
        }
        if use_env_placeholders
        else {
            "FOURPAY_API_URL": base_url,
            "FOURPAY_API_KEY": api_key,
            "FOURPAY_ORGANIZATION_ID": organization_id,
        }
    )
    return {"mcpServers": {"fourpay": {"command": "npx", "args": ["-y", package], "env": env}}}
