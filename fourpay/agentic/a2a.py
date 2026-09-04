"""A2A — Agent2Agent (payment agent).

A2A is how one agent hires another. Your payment agent publishes an **agent
card** at ``/.well-known/agent-card.json`` saying what it can do; a client agent
sends a message and gets back a **task** it polls until it is ``completed``.

A2A carries no money itself — it moves tasks and artifacts. What this module
does is expose FourPay's payment operations as A2A skills.

Tasks live in memory here. A real deployment persists them: A2A clients are
entitled to poll a task id long after the process that created it is gone.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from ..client import is_final_status
from ..errors import TransactionRejectedError
from ..money import format_amount

__all__ = ["A2APaymentAgent", "build_agent_card"]

PROTOCOL_VERSION = "0.3.0"


def build_agent_card(url: str, name: Optional[str] = None, description: Optional[str] = None) -> Dict[str, Any]:
    """The card a client agent fetches from ``/.well-known/agent-card.json``."""
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "name": name or "FourPay Payments Agent",
        "description": description
        or "Creates payments, reports their status and issues refunds on the 4pay.online platform.",
        "url": url,
        "version": "0.1.0",
        "capabilities": {"streaming": False, "pushNotifications": False},
        "defaultInputModes": ["text/plain", "application/json"],
        "defaultOutputModes": ["text/plain", "application/json"],
        "skills": [
            {
                "id": "create_payment",
                "name": "Create a payment",
                "description": "Creates a payment and returns a link the payer opens. Amount is in minor units.",
                "tags": ["payments", "checkout"],
            },
            {
                "id": "payment_status",
                "name": "Check payment status",
                "description": "Reports where a payment stands, by its platform id.",
                "tags": ["payments", "status"],
            },
            {
                "id": "refund_payment",
                "name": "Refund a payment",
                "description": "Refunds a settled payment in full or in part.",
                "tags": ["payments", "refunds"],
            },
        ],
    }


class A2APaymentAgent:
    """A2A server over FourPay: ``message/send``, ``tasks/get``, ``tasks/cancel``."""

    def __init__(
        self,
        client,
        env: str,
        url: str,
        currency: Optional[str] = None,
        return_url: Optional[str] = None,
        fail_url: Optional[str] = None,
        max_amount: Optional[str] = None,
        name: Optional[str] = None,
        description: Optional[str] = None,
    ) -> None:
        self.client = client
        self.env = env
        self.currency = currency
        self.return_url = return_url
        self.fail_url = fail_url
        #: An agent endpoint is reachable by whoever holds its URL. Without a
        #: ceiling, one malformed peer request is an unbounded charge.
        self.max_amount = max_amount
        self.agent_card = build_agent_card(url, name, description)
        self._tasks: Dict[str, Dict[str, Any]] = {}

    def handle(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """Handle one JSON-RPC request; returns the response body to send back."""
        request_id = request.get("id")
        method = request.get("method")

        try:
            if method == "message/send":
                return _ok(request_id, self._on_message(request.get("params") or {}))
            if method == "tasks/get":
                return _ok(request_id, self._require_task(str((request.get("params") or {}).get("id", ""))))
            if method == "tasks/cancel":
                task = self._require_task(str((request.get("params") or {}).get("id", "")))
                if task["status"]["state"] not in ("completed", "failed", "canceled"):
                    task["status"] = {"state": "canceled", "timestamp": _now()}
                return _ok(request_id, task)
            return _error(request_id, -32601, f"Unknown method {method}")
        except Exception as error:  # noqa: BLE001 — JSON-RPC turns errors into responses
            return _error(request_id, -32603, str(error))

    # ---------------------------------------------------------------- internals

    def _on_message(self, params: Dict[str, Any]) -> Dict[str, Any]:
        message = params.get("message")
        if not message or not message.get("parts"):
            raise ValueError("message/send needs a message with at least one part.")

        task = {
            "id": str(uuid.uuid4()),
            "contextId": str(params.get("contextId") or uuid.uuid4()),
            "status": {"state": "working", "timestamp": _now()},
            "artifacts": [],
            "history": [message],
        }
        self._tasks[task["id"]] = task

        data = next((p.get("data", {}) for p in message["parts"] if p.get("kind") == "data"), {})
        skill = data.get("skill")

        try:
            if skill == "create_payment":
                self._create_payment(task, data)
            elif skill == "payment_status":
                self._payment_status(task, data)
            elif skill == "refund_payment":
                self._refund_payment(task, data)
            else:
                task["status"] = {
                    "state": "input-required",
                    "timestamp": _now(),
                    "message": _agent_text(
                        "Name a skill in the data part: create_payment, payment_status or refund_payment."
                    ),
                }
        except Exception as error:  # noqa: BLE001 — a failed task, not a crashed server
            task["status"] = {
                "state": "failed",
                "timestamp": _now(),
                "message": _agent_text(
                    f"Payment refused: {error.reason}"
                    if isinstance(error, TransactionRejectedError)
                    else str(error)
                ),
            }

        return task

    def _create_payment(self, task: Dict[str, Any], data: Dict[str, Any]) -> None:
        amount = str(data.get("amount", ""))
        currency = str(data.get("currency") or self.currency or "")
        if not amount or not currency:
            raise ValueError("create_payment needs amount and currency.")
        if self.max_amount and int(amount) > int(self.max_amount):
            raise ValueError(
                f"Amount {amount} exceeds this agent's ceiling of {self.max_amount} minor units."
            )

        transaction, url = self.client.create_hosted_payment(
            amount=amount,
            currency=currency,
            env=self.env,
            txid=str(data["order_id"]) if data.get("order_id") else None,
            description=str(data["description"]) if data.get("description") else None,
            returnUrl=self.return_url,
            failUrl=self.fail_url,
            merchant_meta={"a2a": {"task_id": task["id"], "context_id": task["contextId"]}},
        )

        task["artifacts"].append({
            "artifactId": str(uuid.uuid4()),
            "name": "payment",
            "parts": [
                {"kind": "text", "text": f"Payment link for {format_amount(amount, currency)}: {url}"},
                {
                    "kind": "data",
                    "data": {
                        "transaction_id": transaction["id"],
                        "status": transaction["status"],
                        "payment_url": url,
                        "amount": amount,
                        "currency": currency,
                        # The link is a short-lived JWT. An agent that caches it
                        # hands the buyer a dead page a quarter of an hour later.
                        "payment_url_expires_in_seconds": 900,
                    },
                },
            ],
        })
        task["status"] = {"state": "completed", "timestamp": _now()}

    def _payment_status(self, task: Dict[str, Any], data: Dict[str, Any]) -> None:
        transaction_id = str(data.get("transaction_id", ""))
        if not transaction_id:
            raise ValueError("payment_status needs transaction_id.")

        tx = self.client.get_transaction(transaction_id)
        detail = f" ({tx['error_description']})" if tx.get("error_description") else ""
        task["artifacts"].append({
            "artifactId": str(uuid.uuid4()),
            "name": "status",
            "parts": [
                {"kind": "text", "text": f"Payment {tx['id']} is {tx['status']}{detail}."},
                {
                    "kind": "data",
                    "data": {
                        "transaction_id": tx["id"],
                        "status": tx["status"],
                        "final": is_final_status(tx["status"]),
                        "settled": tx["status"] == "charged",
                        "amount": tx.get("amount"),
                        "currency": tx.get("currency"),
                    },
                },
            ],
        })
        task["status"] = {"state": "completed", "timestamp": _now()}

    def _refund_payment(self, task: Dict[str, Any], data: Dict[str, Any]) -> None:
        transaction_id = str(data.get("transaction_id", ""))
        if not transaction_id:
            raise ValueError("refund_payment needs transaction_id.")

        refund = self.client.refund(transaction_id, data.get("amount"))
        task["artifacts"].append({
            "artifactId": str(uuid.uuid4()),
            "name": "refund",
            "parts": [
                {"kind": "text", "text": f"Refund of {transaction_id} is {refund['status']}."},
                {"kind": "data", "data": {"transaction_id": refund["id"], "status": refund["status"]}},
            ],
        })
        task["status"] = {"state": "completed", "timestamp": _now()}

    def _require_task(self, task_id: str) -> Dict[str, Any]:
        task = self._tasks.get(task_id)
        if task is None:
            raise ValueError(f"No task {task_id}.")
        return task


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _agent_text(text: str) -> Dict[str, Any]:
    return {"role": "agent", "parts": [{"kind": "text", "text": text}], "messageId": str(uuid.uuid4())}


def _ok(request_id, result) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id, code: int, message: str) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
