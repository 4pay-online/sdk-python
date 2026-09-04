"""Payment tools for an LLM that calls functions.

MCP is one way to give an agent these operations; a plain function-calling loop
is another, and it needs no server. :func:`payment_tools` returns the JSON
Schemas to hand a model, :func:`execute_payment_tool` runs what it picks.

Two decisions are baked in, both because the caller is a model:

* **no free-form amounts** — minor units as a string. Letting a model do decimal
  arithmetic on money is how 10.00 becomes 1000.00;
* **an explicit ceiling** — enforced here, before the request leaves. A prompt
  injection that reaches the tool layer should hit a wall it cannot argue with.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..client import is_final_status
from ..errors import TransactionRejectedError
from ..money import format_amount

__all__ = ["payment_tools", "execute_payment_tool"]


def payment_tools(currency: Optional[str] = None, allow_refunds: bool = False) -> List[Dict[str, Any]]:
    """Tool definitions to give the model."""
    currency_note = f" Defaults to {currency} when omitted." if currency else ""

    tools = [
        {
            "name": "create_payment",
            "description": (
                "Create a payment and get a link for the customer to pay. Returns the payment id "
                "and the URL. The URL expires in 15 minutes — give it to the customer "
                "immediately, do not store it."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "amount": {
                        "type": "string",
                        "description": (
                            "Amount in MINOR units as digits only: 2500 means 25.00 in a "
                            "two-decimal currency. Never send a decimal point."
                        ),
                    },
                    "currency": {"type": "string", "description": f"ISO 4217 code, uppercase.{currency_note}"},
                    "order_id": {
                        "type": "string",
                        "description": (
                            "Your order reference. Reusing it returns the existing payment instead "
                            "of creating a second one — always pass it."
                        ),
                    },
                    "description": {"type": "string"},
                    "customer_email": {"type": "string"},
                },
                "required": ["amount", "order_id"],
            },
        },
        {
            "name": "get_payment",
            "description": (
                "Look a payment up by its platform id and report its status. Only 'charged' means "
                "the money arrived."
            ),
            "input_schema": {
                "type": "object",
                "properties": {"payment_id": {"type": "string"}},
                "required": ["payment_id"],
            },
        },
        {
            "name": "list_payments",
            "description": "List recent payments, newest first.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "status": {"type": "string"},
                    "limit": {"type": "integer", "description": "1-100, default 20."},
                },
            },
        },
    ]

    if allow_refunds:
        tools.append({
            "name": "refund_payment",
            "description": (
                "Refund a settled payment, fully or partly. Irreversible — confirm with the human first."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "payment_id": {"type": "string"},
                    "amount": {"type": "string", "description": "Minor units. Omit to refund it all."},
                },
                "required": ["payment_id"],
            },
        })

    return tools


def execute_payment_tool(
    client,
    name: str,
    args: Dict[str, Any],
    env: str,
    currency: Optional[str] = None,
    max_amount: Optional[str] = None,
    allow_refunds: bool = False,
    return_url: Optional[str] = None,
    fail_url: Optional[str] = None,
) -> Dict[str, Any]:
    """Execute a tool the model chose.

    Returns ``{"ok": bool, "content": str, "data": dict}`` instead of raising: a
    model that receives "this payment was refused because the card expired" can
    do something useful with it, whereas a traceback ends the conversation.
    """
    try:
        if name == "create_payment":
            amount = str(args.get("amount", ""))
            money = str(args.get("currency") or currency or "")

            if not amount.isdigit():
                return _fail(
                    f'amount must be digits only, in minor units — "{amount}" is not. '
                    f'For 25.00 send "2500".'
                )
            if not money:
                return _fail("currency is required.")
            if max_amount and int(amount) > int(max_amount):
                return _fail(
                    f"Refused: {format_amount(amount, money)} is above the "
                    f"{format_amount(max_amount, money)} ceiling set for this agent."
                )

            transaction, url = client.create_hosted_payment(
                amount=amount,
                currency=money,
                env=env,
                txid=str(args["order_id"]),
                description=args.get("description"),
                customer={"email": args["customer_email"]} if args.get("customer_email") else None,
                returnUrl=return_url,
                failUrl=fail_url,
            )
            return {
                "ok": True,
                "content": (
                    f"Payment {transaction['id']} created for {format_amount(amount, money)}. "
                    f"Payment link (valid 15 minutes): {url}"
                ),
                "data": {"payment_id": transaction["id"], "payment_url": url, "status": transaction["status"]},
            }

        if name == "get_payment":
            tx = client.get_transaction(str(args["payment_id"]))
            settled = tx["status"] == "charged"
            detail = f" ({tx['error_description']})" if tx.get("error_description") else ""
            verdict = (
                "The money arrived."
                if settled
                else "Final — no money arrived."
                if is_final_status(tx["status"])
                else "Still in flight."
            )
            return {
                "ok": True,
                "content": f"Payment {tx['id']}: {tx['status']}{detail}. {verdict}",
                "data": {"status": tx["status"], "settled": settled, "final": is_final_status(tx["status"])},
            }

        if name == "list_payments":
            page = client.list_transactions(
                env=env, status=args.get("status"), limit=int(args.get("limit", 20))
            )
            rows = page.get("data", [])
            lines = [
                f"{tx['id']} {tx['status']} {format_amount(tx['amount'], tx['currency'])} {tx.get('txid') or ''}"
                for tx in rows
            ]
            return {"ok": True, "content": "\n".join(lines) or "No payments match.", "data": {"count": len(rows)}}

        if name == "refund_payment":
            if not allow_refunds:
                return _fail("Refunds are not enabled for this agent.")
            refund = client.refund(str(args["payment_id"]), args.get("amount"))
            return {
                "ok": True,
                "content": f"Refund of {args['payment_id']} is {refund['status']}.",
                "data": {"status": refund["status"]},
            }

        return _fail(f"Unknown tool {name}.")

    except TransactionRejectedError as error:
        hint = (
            " No terminal is configured for this currency and environment — a human has to fix that."
            if error.is_routing_failure
            else ""
        )
        return _fail(f"The platform refused the payment: {error.reason}.{hint}")
    except Exception as error:  # noqa: BLE001 — the model gets a sentence, not a traceback
        return _fail(str(error))


def _fail(content: str) -> Dict[str, Any]:
    return {"ok": False, "content": content}
