# 4pay-sdk

Python client for the [4pay.online](https://4pay.online) payment platform. Python 3.9+, no
runtime dependencies.

The package is not on PyPI. Install it from this repository or from the archive:

```bash
pip install "git+https://github.com/4pay-online/sdk-python.git"
pip install "4pay-sdk[crypto] @ git+https://github.com/4pay-online/sdk-python.git"   # only for AP2 mandate signatures

# or, without GitHub:
pip install https://docs.4pay.online/sdk/4pay-sdk-python.tar.gz
```

```python
from fourpay import FourPay

client = FourPay(
    api_key=os.environ["FOURPAY_API_KEY"],
    organization_id=os.environ["FOURPAY_ORGANIZATION_ID"],   # issued together with the key
    base_url="https://sandbox.4pay.online",
)

transaction, payment_url = client.create_hosted_payment(
    amount="150.00",        # or "15000" in minor units — both work
    currency="USD",
    env="test",
    txid="order-8891",      # your order id, and the idempotency key
    returnUrl="https://shop.example/thanks",
    failUrl="https://shop.example/sorry",
)
```

## Webhooks

```python
from fourpay import EventDeduplicator, WebhookVerificationError, verify_webhook

seen = EventDeduplicator()

@app.route("/webhooks/payment", methods=["POST"])
def webhook():
    try:
        event = verify_webhook(request.get_data(), dict(request.headers), SECRET)
    except WebhookVerificationError:
        return "", 401

    if seen.accept(event["event_id"]) and event["data"]["status"] == "charged":
        fulfil(event["data"]["txid"])
    return "", 200
```

`request.get_data()`, never `request.json`: the signature covers the raw bytes.

## Development

```bash
python -m pytest tests/
```

## What it handles for you

| The raw API does this | The SDK does this |
|---|---|
| `hosted` has no default; without it a payment is refused as `unexpected card` | the hosted helper sets it |
| `201 Created` carries refusals (`rejected`, `failed`) | raises, with a flag telling "no terminal" from "card declined" |
| a missing `x-organization-id` gives `400`, not `401` | both headers always sent |
| timestamps like `2026-09-03 11:18:30.101839 Etc/UTC` | parsed correctly, always UTC |
| paging is `cursor`; `start_cursor` is silently ignored | follows the real cursor, and stops if it stops moving |
| a retried create without `txid` charges twice | only idempotent writes are retried |
| money as decimal strings in minor units | integer arithmetic throughout — never floats |

## Agentic commerce

`agentic/` carries the merchant side of MCP, AP2, ACP and A2A — see
[Agentic Payments](https://docs.4pay.online/docs/partners/agentic-payments).

## Documentation

[docs.4pay.online/docs/partners/sdk](https://docs.4pay.online/docs/partners/sdk)

## Licence

MIT.
