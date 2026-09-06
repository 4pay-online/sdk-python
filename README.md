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
    organization_id=os.environ["FOURPAY_ORGANIZATION_ID"],   # issued with the key; required for keys
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

## Naming the organization

Every call has to say which organization it acts in, and there are two ways to say it.

**Pass `organization_id`.** The SDK sends it as `x-organization-id` on every call — including
`create_session`, which is unauthenticated and still needs it, because a person is looked up
in that organization's own schema. An API key always goes this way: it is issued together
with its `organization_id`, and the platform refuses the request before it ever reads the key.

**Or set `organization_from_perimeter`.** A partner integrating on a domain of their own does
not have to know the id: the proxy in front of that domain pins `x-organization-id` from the
vhost, and the SDK adds nothing. That is a statement about your deployment, which is why you
make it — the SDK cannot see your proxy and does not guess.

The constructor insists on one of the two — and on `organization_id` itself when you pass an
API key. A session token with neither names no organization at all, and is refused before any
request goes out.

The option is for a domain of **yours**. On ours — `4pay.online` and everything under it,
including `sandbox.4pay.online` — it is refused: those hosts serve every organization and pin
nothing. Ours is a question about the host: `https://4pay.online/api`,
`https://4pay.online:443` and `https://4pay.online` are the same host spelled three ways.

```python
# Your domain, your proxy, your organization — no organization_id needed.
client = FourPay(
    bearer_token=token,
    base_url="https://pay.partner.example",
    organization_from_perimeter=True,
)
```

## Logging in

An API key is not the only way in. `FourPay.for_login()` builds a client that has no
credential yet — because a login and a password are what you have — and `create_session`
turns it into an ordinary client carrying the session token.

```python
client = FourPay.for_login(base_url="https://sandbox.4pay.online")
session = client.create_session("admin@example.com", "secret", "admin")   # or "client", "partner"

client.list_transactions(env="test")     # the token from that session travels on
```

`expire_at` is Unix **seconds**, and the field is singular; there is no `expires_at`.

Who needs an `organization_id` to log in:

| Account type | Where the platform looks | `organization_id` |
|---|---|---|
| `admin`, `client` | the platform's own schema | not needed |
| `partner` | searched for across organizations | not needed |
| `person` | that one organization's schema | **required** (or `organization_from_perimeter`) |

A `person` login without one is refused by the SDK: the platform would answer a flat `401`,
which reads like a wrong password.

An admin session names no organization of its own. To act inside one, say which:

```python
admin = FourPay(bearer_token=session["token"], organization_id=org_id)
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
| a missing `x-organization-id` gives `400`, not `401` | insists on an `organization_id` or a custom `base_url`, and sends the header whenever it has one |
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
