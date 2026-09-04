import pytest

from fourpay import FourPay, FourPayAuthError, TransactionRejectedError

from conftest import CREATED


def test_an_api_key_without_an_organization_id_is_refused_before_any_request():
    with pytest.raises(ValueError, match="organization_id"):
        FourPay(api_key="key-1")


def test_both_credential_headers_travel_on_every_call(stub):
    client, transport = stub(lambda n: {"body": CREATED})
    client.get_transaction("tx-1")

    assert transport.calls[0]["headers"]["x-api-key"] == "key-1"
    assert transport.calls[0]["headers"]["x-organization-id"] == "org-1"


def test_create_hosted_payment_sets_hosted_because_the_platform_has_no_default(stub):
    client, transport = stub(lambda n: {"status": 201, "body": CREATED})
    _, url = client.create_hosted_payment(amount="10.00", currency="USD", env="test", txid="order-1")

    sent = transport.calls[0]["body"]["params"]
    assert sent["hosted"] is True
    assert sent["amount"] == "1000"
    assert url == CREATED["widget_url"]


def test_201_created_carrying_a_rejected_transaction_raises(stub):
    client, _ = stub(lambda n: {
        "status": 201,
        "body": {**CREATED, "status": "rejected", "error_description": "terminal not found"},
    })

    with pytest.raises(TransactionRejectedError) as caught:
        client.create_payment(amount="1000", currency="USD", env="test")

    assert caught.value.is_routing_failure


def test_a_create_without_an_idempotency_key_is_never_retried(stub):
    client, transport = stub(lambda n: {"status": 503, "body": {"errors": [{"detail": "busy"}]}})

    with pytest.raises(Exception):
        client.create_payment(amount="1000", currency="USD", env="test")

    assert len(transport.calls) == 1


def test_a_create_with_a_txid_is_retried(stub):
    client, transport = stub(
        lambda n: {"status": 503, "body": {"errors": [{"detail": "busy"}]}} if n < 3
        else {"status": 201, "body": CREATED}
    )
    assert client.create_payment(amount="1000", currency="USD", env="test", txid="order-1")["id"] == "tx-1"
    assert len(transport.calls) == 3


def test_a_bare_400_is_surfaced_as_an_auth_problem(stub):
    client, _ = stub(lambda n: {"status": 400, "body": {"errors": [{"detail": "Bad Request"}]}})
    with pytest.raises(FourPayAuthError):
        client.get_transaction("tx-1")


def test_env_must_be_in_the_body_and_the_sdk_refuses_to_guess(stub):
    client, _ = stub(lambda n: {"status": 201, "body": CREATED})
    with pytest.raises(ValueError, match="PRODUCTION transaction"):
        client.create_payment(amount="1000", currency="USD")


def test_a_server_to_server_payment_without_redirect_urls_is_stopped_before_it_creates_debris(stub):
    client, transport = stub(lambda n: {"status": 201, "body": CREATED})
    with pytest.raises(ValueError, match="returnUrl and failUrl"):
        client.create_payment(
            amount="1000", currency="RUB", env="test", txid="o-1",
            money_storage={"cardnumber": "2201382000000013"},
        )
    assert transport.calls == []


def test_iteration_follows_the_cursor_and_stops_when_it_stops_moving(stub):
    pages = [
        {"data": [{"id": "a"}, {"id": "b"}], "cursor": {"cursor_field": "started", "cursor_value": "t2"}},
        {"data": [{"id": "c"}], "cursor": {"cursor_field": "started", "cursor_value": "t3"}},
        {"data": [], "cursor": {"cursor_field": "started", "cursor_value": None}},
    ]
    client, transport = stub(lambda n: {"body": pages[n - 1]})

    assert [tx["id"] for tx in client.iter_transactions(env="test")] == ["a", "b", "c"]
    assert "cursor=t2" in transport.calls[1]["url"]


def test_a_repeating_cursor_ends_the_walk_instead_of_looping_for_ever(stub):
    page = {"data": [{"id": "a"}], "cursor": {"cursor_field": "started", "cursor_value": "same"}}
    client, transport = stub(lambda n: {"body": page})

    assert [tx["id"] for tx in client.iter_transactions(env="test")] == ["a", "a"]
    assert len(transport.calls) == 2


def test_health_reports_core_status_since_nothing_is_called_status(stub):
    client, transport = stub(lambda n: {"body": {"core_status": "ok", "appversion": "f0175d14"}})
    health = client.health()

    assert health["core_status"] == "ok"
    assert "status" not in health
    assert "x-api-key" not in transport.calls[0]["headers"]
