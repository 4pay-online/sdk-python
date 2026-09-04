import json
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fourpay import FourPay  # noqa: E402


class StubTransport:
    """Records requests and replays canned responses, without touching the network."""

    def __init__(self, handler: Callable[[int], Dict[str, Any]]):
        self.handler = handler
        self.calls: List[Dict[str, Any]] = []

    def __call__(self, request, timeout=None):
        import urllib.error

        body = json.loads(request.data.decode()) if request.data else None
        self.calls.append({"url": request.full_url, "method": request.method,
                           "headers": {k.lower(): v for k, v in request.headers.items()},
                           "body": body})

        response = self.handler(len(self.calls)) or {}
        status = response.get("status", 200)
        payload = json.dumps(response.get("body", {})).encode()

        if status >= 400:
            raise urllib.error.HTTPError(request.full_url, status, "error", {}, _Reader(payload))
        return _Response(payload)


class _Response:
    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _Reader:
    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self):
        return self._payload

    def close(self):
        return None


@pytest.fixture
def stub(monkeypatch):
    def build(handler):
        transport = StubTransport(handler)
        monkeypatch.setattr("fourpay.client.urllib.request.urlopen", transport)
        client = FourPay(
            api_key="key-1", organization_id="org-1",
            base_url="https://sandbox.4pay.online", max_retries=2,
        )
        monkeypatch.setattr("fourpay.client.time.sleep", lambda _: None)
        return client, transport
    return build


CREATED = {
    "id": "tx-1", "status": "created", "type": "payment", "env": "test",
    "amount": "1000", "currency": "USD", "txid": "order-1",
    "widget_url": "https://pay.sandbox.4pay.online?id=tx-1&token=jwt",
}
