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
def transport(monkeypatch):
    """Installs the stub transport and hands it back; the test builds its own client.

    ``stub`` below builds a keyed client, which is what most tests want. The
    ones about how a client comes into being cannot use it — the client is the
    thing under test.
    """
    def install(handler):
        recorder = StubTransport(handler)
        monkeypatch.setattr("fourpay.client.urllib.request.urlopen", recorder)
        monkeypatch.setattr("fourpay.client.time.sleep", lambda _: None)
        return recorder
    return install


@pytest.fixture
def stub(transport):
    def build(handler, **overrides):
        recorder = transport(handler)
        options = {
            "api_key": "key-1", "organization_id": "org-1",
            "base_url": "https://sandbox.4pay.online", "max_retries": 2,
        }
        options.update(overrides)
        return FourPay(**options), recorder
    return build


CREATED = {
    "id": "tx-1", "status": "created", "type": "payment", "env": "test",
    "amount": "1000", "currency": "USD", "txid": "order-1",
    "widget_url": "https://pay.sandbox.4pay.online?id=tx-1&token=jwt",
}
