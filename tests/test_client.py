import pytest

from fourpay import FourPay, FourPayAuthError, TransactionRejectedError

from conftest import CREATED


def test_an_api_key_without_an_organization_id_is_refused_before_any_request():
    with pytest.raises(ValueError, match="organization_id"):
        FourPay(api_key="key-1")


def test_an_api_key_is_refused_without_an_organization_id_even_on_a_custom_domain():
    # The custom-domain path is for session tokens. A key is issued together
    # with its organization_id, so there is no reason to guess at one.
    with pytest.raises(ValueError, match="organization_id"):
        FourPay(api_key="key-1", base_url="https://pay.partner.example")


def test_a_bearer_token_on_the_shared_host_needs_an_organization_id():
    # Nothing on the default host says which organization the session acts in.
    with pytest.raises(ValueError, match="organization_id"):
        FourPay(bearer_token="token-1")


def test_a_bearer_token_behind_a_perimeter_needs_no_organization_id():
    # The partner states it: their own proxy pins x-organization-id from the
    # vhost, so the SDK has nothing to add.
    client = FourPay(
        bearer_token="token-1",
        base_url="https://pay.partner.example",
        organization_from_perimeter=True,
    )

    assert client.organization_id is None


@pytest.mark.parametrize(
    "base_url",
    [
        "https://sandbox.4pay.online",
        "https://api.4pay.online",
        "https://pay.4pay.online",
        "https://pay.sandbox.4pay.online",
    ],
)
def test_our_own_hosts_other_than_the_default_still_need_an_organization_id(base_url):
    # The bug this option replaced. The SDK used to infer "own domain" from one
    # literal host, so every OTHER host of ours counted as somebody's own — the
    # sandbox included. A session token on the sandbox with no organization_id
    # was accepted, and every call it made carried no organization at all.
    with pytest.raises(ValueError, match="organization_id"):
        FourPay(bearer_token="token-1", base_url=base_url)


@pytest.mark.parametrize(
    "base_url",
    ["https://4pay.online", "https://sandbox.4pay.online", "https://api.4pay.online"],
)
def test_claiming_a_perimeter_on_our_own_host_is_refused(base_url):
    # Stating it does not make it so. Nothing in front of our hosts pins the
    # header, so the claim is a misunderstanding — and left standing it would
    # send the same organization-less calls the option exists to prevent.
    with pytest.raises(ValueError, match="that host is ours"):
        FourPay(bearer_token="token-1", base_url=base_url, organization_from_perimeter=True)


def test_a_blank_organization_id_is_no_organization_id():
    # Spaces would sail through a check for None and turn the gate into a
    # formality — the failure would surface as a platform error on the first call.
    with pytest.raises(ValueError, match="organization_id"):
        FourPay(api_key="key-1", organization_id="   ")


def test_a_padded_organization_id_travels_trimmed(stub):
    client, transport = stub(lambda n: {"body": CREATED}, organization_id="  org-1  ")
    client.get_transaction("tx-1")

    assert transport.calls[0]["headers"]["x-organization-id"] == "org-1"


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


def test_create_session_carries_the_organization_without_carrying_a_credential(stub):
    # The login itself is unauthenticated, but it is not organization-agnostic:
    # a person is looked up in that organization's own schema. Sending the
    # credential headers here would be pointless; sending the organization is not.
    client, transport = stub(lambda n: {"body": {"token": "session-1", "expire_at": 1}})
    client.create_session("partner@example.com", "secret")

    headers = transport.calls[0]["headers"]
    assert headers["x-organization-id"] == "org-1"
    assert "x-api-key" not in headers
    assert "authorization" not in headers


# --- the shared host is a host, not a spelling of one ------------------------


def test_a_session_on_the_shared_host_is_built_when_it_names_its_organization():
    # The legitimate shared-host case, and the one the gate exists to allow.
    client = FourPay(bearer_token="token-1", organization_id="org-1")

    assert client.base_url == "https://4pay.online"
    assert client.organization_id == "org-1"


def test_the_same_session_without_an_organization_id_is_not_built():
    with pytest.raises(ValueError, match="organization_id"):
        FourPay(bearer_token="token-1")


@pytest.mark.parametrize(
    "base_url",
    [
        "https://4pay.online/api",
        "https://4pay.online/",
        "https://4pay.online:443",
        "https://someone:secret@4pay.online",
        "HTTPS://4PAY.ONLINE",
        "https://sandbox.4pay.online:8443/api",
    ],
)
def test_our_host_spelled_differently_is_still_our_host(base_url):
    # A path, a port, credentials, capitals — the host is the same host, and
    # "is this one of ours?" is a question only the host answers.
    with pytest.raises(ValueError, match="that host is ours"):
        FourPay(bearer_token="token-1", base_url=base_url, organization_from_perimeter=True)


@pytest.mark.parametrize(
    "base_url",
    [
        "https://pay.partner.example",
        "https://pay.partner.example/gateway",
        "https://4pay.online.partner.example",
        "https://not4pay.online",
    ],
)
def test_a_partner_host_that_only_looks_like_ours_is_a_partner_host(base_url):
    # A name that merely ENDS in something of ours is not ours: the check is on
    # the dot boundary, not on a substring.
    client = FourPay(
        bearer_token="token-1", base_url=base_url, organization_from_perimeter=True
    )

    assert client.organization_id is None


def test_a_trailing_slash_on_the_base_url_does_not_double_up_in_the_path(stub):
    # The URL is built by concatenation, so the normalization is what keeps
    # "//api/v1/..." out of the request line.
    client, transport = stub(lambda n: {"body": CREATED}, base_url="https://sandbox.4pay.online///")
    client.get_transaction("tx-1")

    assert client.base_url == "https://sandbox.4pay.online"
    assert transport.calls[0]["url"] == "https://sandbox.4pay.online/api/v1/transactions/tx-1"


# --- logging in, which starts without a credential ---------------------------


def test_a_login_client_is_built_without_a_credential_because_that_is_the_point():
    client = FourPay.for_login(organization_id="org-1", base_url="https://sandbox.4pay.online")

    assert client.api_key is None
    assert client.bearer_token is None


def test_an_admin_logs_in_on_the_shared_host_with_no_organization_to_name(transport):
    # An admin lives in the platform's own schema, not in an organization's, so
    # there is nothing to put in x-organization-id — and the login still works.
    calls = transport(lambda n: {"body": {"token": "session-1", "expire_at": 1}})
    FourPay.for_login().create_session("admin@example.com", "secret", "admin")

    assert calls.calls[0]["url"] == "https://4pay.online/api/v1/session"
    assert "x-organization-id" not in calls.calls[0]["headers"]


def test_a_login_hands_the_client_the_session_token_it_got(transport):
    calls = transport(
        lambda n: {"body": {"token": "session-1", "expire_at": 1}} if n == 1 else {"body": CREATED}
    )
    client = FourPay.for_login(organization_id="org-1", base_url="https://sandbox.4pay.online")
    session = client.create_session("person@example.com", "secret", "person")
    client.get_transaction("tx-1")

    assert session["token"] == "session-1"
    assert calls.calls[0]["headers"]["x-organization-id"] == "org-1"
    assert "authorization" not in calls.calls[0]["headers"]
    assert calls.calls[1]["headers"]["authorization"] == "Bearer session-1"


def test_a_person_login_with_no_organization_is_stopped_before_the_flat_401(transport):
    # The platform looks a person up in one organization's schema. With nothing
    # to look in it answers 401, which reads as a wrong password.
    calls = transport(lambda n: {"body": {}})

    with pytest.raises(ValueError, match="person login"):
        FourPay.for_login().create_session("person@example.com", "secret", "person")

    assert calls.calls == []


def test_a_login_client_refuses_an_ordinary_call_until_it_has_a_session(transport):
    calls = transport(lambda n: {"body": CREATED})
    client = FourPay.for_login(organization_id="org-1", base_url="https://sandbox.4pay.online")

    with pytest.raises(ValueError, match="no credential yet"):
        client.get_transaction("tx-1")

    assert calls.calls == []
