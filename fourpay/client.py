"""Client for the partner-facing API."""

from __future__ import annotations

import json
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Iterator, Mapping, Optional

from .errors import (
    PaymentUrlUnavailableError,
    FourPayConnectionError,
    FourPayRateLimitError,
    FourPayTimeoutError,
    TransactionRejectedError,
    error_from_response,
)
from .money import to_minor_units

__all__ = ["FourPay", "FINAL_STATUSES", "is_final_status", "is_settled"]

SDK_VERSION = "0.1.0"
DEFAULT_BASE_URL = "https://4pay.online"

#: Statuses after which nothing more happens on its own.
FINAL_STATUSES = frozenset({"charged", "rejected", "failed", "reversed"})


def is_final_status(status: str) -> bool:
    """Is this the end of the road?

    An unknown status counts as **non-final** — the safe answer. Rails carry
    their own intermediate states, and a closed list would strand a payment
    that is still moving.
    """
    return status in FINAL_STATUSES


def is_settled(status: str) -> bool:
    """Did the money actually move? Only ``charged`` means yes."""
    return status == "charged"


def _trimmed(value: Any) -> Optional[str]:
    """Blank is absent.

    A string of spaces is neither a credential nor an organization. Accepting
    one would make the constructor's checks a formality and move the failure to
    the first HTTP call, where a typo in an environment variable reads as a
    platform fault.
    """
    if value is None:
        return None
    text = value.strip() if isinstance(value, str) else str(value).strip()
    return text or None


def _host(url: str) -> str:
    """The host of a base URL — no scheme, no userinfo, no port, no path.

    "Is this a domain of my own?" is a question about the host, and only the
    host answers it. A URL that carries the same host with a path, a port or
    credentials in front of it is the same host.
    """
    parsed = urllib.parse.urlsplit(url if "//" in url else "//" + url)
    return (parsed.hostname or "").lower()


def _is_shared_host(host: str) -> bool:
    """Hosts we run ourselves: the apex and everything under it.

    Each serves every organization and names none — the sandbox, the API host
    and the payment host included. The check exists to refuse a claim that
    cannot be true: ``organization_from_perimeter`` says a proxy in front of
    this host pins ``x-organization-id`` for one organization, and in front of
    ours nothing pins it.
    """
    return host == "4pay.online" or host.endswith(".4pay.online")


class FourPay:
    """Partner client.

    ::

        pay = FourPay(
            api_key=os.environ["FOURPAY_API_KEY"],
            organization_id=os.environ["FOURPAY_ORGANIZATION_ID"],
            base_url="https://sandbox.4pay.online",
        )
        tx, url = fourpay.create_hosted_payment(
            amount="10.00", currency="USD", env="test", txid="order-1",
        )

    Every call has to say which organization it acts in, and there are two
    ways to say it. Either pass ``organization_id`` — the SDK then sends it as
    ``x-organization-id`` on every call — or set
    ``organization_from_perimeter`` when your own proxy pins that header for
    you, and the SDK adds nothing. An API key always takes the first way: it
    is issued together with its ``organization_id``.

    No credential yet, only a login and a password? :meth:`for_login`.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        organization_id: Optional[str] = None,
        bearer_token: Optional[str] = None,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 30.0,
        max_retries: int = 2,
        user_agent: Optional[str] = None,
        organization_from_perimeter: bool = False,
        _login_only: bool = False,
    ) -> None:
        api_key = _trimmed(api_key)
        bearer_token = _trimmed(bearer_token)
        organization_id = _trimmed(organization_id)
        base_url = (_trimmed(base_url) or DEFAULT_BASE_URL).rstrip("/")

        # Whether the proxy in front of base_url pins x-organization-id is a
        # statement about the caller's deployment, not something the SDK can
        # see — so the caller makes it and the SDK does not guess.
        #
        # It used to guess, by comparing the host against the one default, and
        # the guess was wrong for every 4pay host but that one: with base_url on
        # the sandbox, a session token and no organization_id, the client was
        # built naming no organization and every call it made landed in none.
        from_perimeter = bool(organization_from_perimeter)

        if not api_key and not bearer_token and not _login_only:
            raise ValueError(
                "Pass api_key or bearer_token — there is no anonymous access. Arriving with a "
                "login and a password instead? FourPay.for_login() builds the client that has no "
                "credential yet, and its one purpose is to call create_session()."
            )
        if api_key and not organization_id:
            raise ValueError(
                "An API key without organization_id is not a credential: the platform resolves the "
                "key inside the organization named by the x-organization-id header, and refuses the "
                "request before reading the key. Your operator issues both values together. The "
                "other way to name an organization — organization_from_perimeter, where your own "
                "proxy pins that header — belongs to session tokens; this SDK does not take it in "
                "place of organization_id for a key."
            )
        # The claim cannot be true on a host we run: none of ours pins the
        # header, and a client built on the claim would send no organization at
        # all. Better caught here than as a 400 from the first call, which reads
        # as our fault.
        if from_perimeter and _is_shared_host(_host(base_url)):
            raise ValueError(
                f"organization_from_perimeter says a proxy in front of {base_url} pins "
                "x-organization-id for one organization, but that host is ours: it serves every "
                "organization and pins nothing. The option is for a domain of your own. On our "
                "hosts, pass organization_id."
            )
        if not organization_id and not from_perimeter and not _login_only:
            raise ValueError(
                "A session token still has to say which organization it acts in, and there are two "
                "ways to say it: pass organization_id, or set organization_from_perimeter when "
                "your own proxy pins x-organization-id for you. Neither is set, so the call would "
                "reach no organization at all."
            )

        self.api_key = api_key
        self.organization_id = organization_id
        self.bearer_token = bearer_token
        self.base_url = base_url
        self.timeout = timeout
        self.max_retries = max_retries
        self.user_agent = f"4pay-sdk-python/{SDK_VERSION}" + (f" {user_agent}" if user_agent else "")
        self._organization_from_perimeter = from_perimeter

    @classmethod
    def for_login(
        cls,
        organization_id: Optional[str] = None,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 30.0,
        max_retries: int = 2,
        user_agent: Optional[str] = None,
        organization_from_perimeter: bool = False,
    ) -> "FourPay":
        """A client with no credential yet, whose one purpose is to log in.

        ::

            pay = FourPay.for_login(base_url="https://sandbox.4pay.online")
            session = pay.create_session("admin@example.com", "secret", "admin")
            # from here the client carries session["token"] like any other

        The constructor insists on a credential, and login is exactly where you
        do not have one. ``organization_id`` is optional here, and only here:
        admins and clients live in the platform's own schema and are found
        without it, and a partner is searched for across organizations. A
        ``person`` is not — see :meth:`create_session`.

        Anything other than :meth:`create_session` and :meth:`health` raises
        until the session token arrives; an unauthenticated call would only
        come back ``401``.
        """
        return cls(
            organization_id=organization_id,
            base_url=base_url,
            timeout=timeout,
            max_retries=max_retries,
            user_agent=user_agent,
            organization_from_perimeter=organization_from_perimeter,
            _login_only=True,
        )

    # ---------------------------------------------------------------- payments

    def create_hosted_payment(self, **params: Any):
        """Create a payment and return ``(transaction, payment_url)``.

        Sets ``hosted`` for you. Left to itself the platform has no hosted
        default: a create without it is read as server-to-server, finds no
        card, and comes back ``201 Created`` with ``status="rejected"``.

        The URL carries a JWT that expires in about 15 minutes. Redirect
        promptly; do not store it.
        """
        params["hosted"] = True
        transaction = self.create_payment(**params)

        url = transaction.get("widget_url")
        if not url:
            raise PaymentUrlUnavailableError(transaction)
        return transaction, url

    def create_payment(self, **params: Any) -> Dict[str, Any]:
        """Create a payment.

        ``amount`` may be minor units (``"1000"``, ``1000``) or a decimal string
        in major units (``"10.00"``). Raises :class:`TransactionRejectedError`
        when the platform refuses the payment, because it says so with
        ``201 Created`` and a ``rejected`` body.
        """
        params.setdefault("type", "payment")
        self._assert_environment(params.get("env"))
        self._assert_redirect_urls(params)

        transaction = self._request(
            "POST", "/api/v1/transactions",
            body={"params": self._normalize_amount(params)},
            idempotent=bool(params.get("txid")),
        )
        if transaction.get("status") in ("rejected", "failed"):
            raise TransactionRejectedError(transaction)
        return transaction

    def create_payout(self, **params: Any) -> Dict[str, Any]:
        """Create a payout. Same rules as :meth:`create_payment`; no hosted page."""
        params["type"] = "payout"
        self._assert_environment(params.get("env"))

        transaction = self._request(
            "POST", "/api/v1/transactions",
            body={"params": self._normalize_amount(params)},
            idempotent=bool(params.get("txid")),
        )
        if transaction.get("status") in ("rejected", "failed"):
            raise TransactionRejectedError(transaction)
        return transaction

    def get_transaction(self, transaction_id: str) -> Dict[str, Any]:
        """Read one transaction by its platform id (a UUID, not your ``txid``)."""
        return self._request("GET", f"/api/v1/transactions/{urllib.parse.quote(transaction_id)}")

    def list_transactions(self, **params: Any) -> Dict[str, Any]:
        """One page of transactions, plus the cursor that opens the next one.

        Only the documented filters exist — ``page``, ``offset``, ``from``,
        ``to`` and ``currency`` are refused with ``422``. Paging is ``cursor``;
        ``start_cursor`` is accepted and then ignored.
        """
        return self._request("GET", "/api/v1/transactions", query=params)

    def iter_transactions(self, **params: Any) -> Iterator[Dict[str, Any]]:
        """Walk every transaction matching the filter, following the cursor.

        Stops on an empty page or a cursor that stops moving — a repeating
        cursor means the filter was silently dropped, and looping on it would
        never end.
        """
        cursor = params.pop("cursor", None)
        previous = None

        while True:
            page = self.list_transactions(**({**params, "cursor": cursor} if cursor else params))
            rows = page.get("data") or []
            if not rows:
                return
            for row in rows:
                yield row

            nxt = (page.get("cursor") or {}).get("cursor_value")
            if not nxt or nxt in (previous, cursor):
                return
            previous, cursor = cursor, nxt

    def aggregate(self, **params: Any) -> Dict[str, Any]:
        """Summed totals for the filter, in the platform's own ``{"aggr": [...]}`` shape."""
        return self._request("GET", "/api/v1/transactions/aggregate", query=params)

    def refund(self, transaction_id: str, amount=None, currency: Optional[str] = None) -> Dict[str, Any]:
        """Refund a settled payment. Omit ``amount`` for the full sum."""
        params: Dict[str, Any] = {"type": "refund"}
        if amount is not None:
            params["amount"] = to_minor_units(amount, currency) if currency else str(amount)
        return self._request(
            "PUT", f"/api/v1/transactions/{urllib.parse.quote(transaction_id)}", body={"params": params}
        )

    def cancel(self, transaction_id: str) -> Dict[str, Any]:
        """Cancel a transaction that has not settled. It ends ``failed``, never ``reversed``."""
        return self._request(
            "PUT", f"/api/v1/transactions/{urllib.parse.quote(transaction_id)}",
            body={"params": {"type": "cancel"}},
        )

    def commit(self, transaction_id: str) -> Dict[str, Any]:
        """Capture a two-phase authorization."""
        return self._request("POST", f"/api/v1/transactions/commit/{urllib.parse.quote(transaction_id)}")

    def submit_auth_params(self, transaction_id: str, params: Mapping[str, Any]) -> Dict[str, Any]:
        """Submit what the payer produced for ``action_required`` — an OTP, a 3DS result."""
        return self._request(
            "POST",
            f"/api/v1/transactions/additional_params_for_auth/{urllib.parse.quote(transaction_id)}",
            body={"params": dict(params)},
        )

    def wait_for_completion(
        self, transaction_id: str, timeout: float = 180.0, interval: float = 3.0, on_update=None
    ) -> Dict[str, Any]:
        """Poll until the transaction reaches a final status.

        A convenience for scripts, tests and agents. Production integrations
        should take the webhook instead — polling costs a request per interval
        and still lags the event.
        """
        deadline = time.monotonic() + timeout
        transaction = None

        while True:
            transaction = self.get_transaction(transaction_id)
            if on_update:
                on_update(transaction)
            if is_final_status(transaction.get("status", "")):
                return transaction
            if time.monotonic() + interval > deadline:
                raise FourPayTimeoutError(
                    f"Transaction {transaction_id} was still {transaction.get('status')} "
                    f"after {int(timeout)}s",
                    transaction,
                )
            time.sleep(interval)

    # ------------------------------------------------------------------ system

    def health(self) -> Dict[str, Any]:
        """Platform health. The readiness field is ``core_status`` — there is no ``status``."""
        return self._request("GET", "/api/v1/health", auth=False)

    def create_session(self, login: str, password: str, account_type: str = "partner") -> Dict[str, Any]:
        """Exchange login and password for a session token.

        Reachable without a credential through :meth:`for_login`; on a client
        that already holds one it simply replaces the session token.

        ``expire_at`` is Unix **seconds** and the field is singular — code
        written against an ``expires_at`` ISO string reads ``None`` and treats
        the token as immortal.

        A ``person`` is looked up inside one organization's own schema, so that
        login has to name an organization. ``admin``, ``client`` and ``partner``
        do not: the first two live in the platform's own schema, and a partner
        is searched for across organizations.
        """
        if account_type == "person" and not self.organization_id and not self._organization_from_perimeter:
            raise ValueError(
                "A person login has to name an organization: the platform looks the person up in "
                "that organization's own schema, and with nowhere to look it answers a flat 401 "
                "that reads like a wrong password. Pass organization_id, or set "
                "organization_from_perimeter when your own proxy pins x-organization-id for you."
            )

        session = self._request(
            "POST", "/api/v1/session",
            body={"type": account_type, "login": login, "password": password},
            auth=False,
        )
        self.bearer_token = session.get("token")
        return session

    # ---------------------------------------------------------------- plumbing

    def _request(
        self,
        method: str,
        path: str,
        body: Any = None,
        query: Optional[Mapping[str, Any]] = None,
        auth: bool = True,
        idempotent: bool = False,
    ) -> Any:
        """Escape hatch for endpoints the SDK does not wrap yet."""
        if auth and not self.api_key and not self.bearer_token:
            raise ValueError(
                "This client has no credential yet: FourPay.for_login() builds one only to call "
                "create_session(). Call that first — the session token it returns stays on the "
                "client — or build the client with an api_key or a bearer_token."
            )

        url = self.base_url + path
        if query:
            pairs = {k: str(v) for k, v in query.items() if v is not None and v != ""}
            if pairs:
                url += "?" + urllib.parse.urlencode(pairs)

        headers = {"accept": "application/json", "user-agent": self.user_agent}
        if body is not None:
            headers["content-type"] = "application/json"
        if auth:
            if self.api_key:
                headers["x-api-key"] = self.api_key
            if self.bearer_token:
                headers["authorization"] = f"Bearer {self.bearer_token}"
        # Outside the ``auth`` branch on purpose: this header is not a credential,
        # it says which organization the call is about, and the unauthenticated
        # calls need it too. ``create_session`` is the one that matters — a person
        # login is looked up in that organization's own schema, and without the
        # header the platform has nowhere to look. Absent means the caller declared
        # organization_from_perimeter, and the proxy supplies it.
        if self.organization_id:
            headers["x-organization-id"] = self.organization_id

        # A write is retried only when it carries an idempotency key. Repeating
        # a create without one after a timeout is how one order becomes two
        # charges — the platform cannot tell the retry from a new payment.
        attempts = self.max_retries + 1 if method == "GET" or idempotent else 1
        last_error: Optional[Exception] = None

        for attempt in range(attempts):
            if attempt:
                time.sleep(_backoff(attempt, last_error))

            request = urllib.request.Request(
                url, method=method, headers=headers,
                data=json.dumps(body).encode() if body is not None else None,
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    payload = response.read().decode("utf-8")
                    return json.loads(payload) if payload else None
            except urllib.error.HTTPError as http_error:
                payload = http_error.read().decode("utf-8", "replace")
                try:
                    parsed = json.loads(payload) if payload else None
                except json.JSONDecodeError:
                    parsed = {"errors": [{"detail": payload[:500]}]}

                error = error_from_response(http_error.code, parsed, dict(http_error.headers))
                if not (error.retryable and attempt < attempts - 1):
                    raise error
                last_error = error
            except (urllib.error.URLError, TimeoutError, OSError) as cause:
                last_error = FourPayConnectionError(f"{method} {path} failed: {cause}")
                if attempt == attempts - 1:
                    raise last_error from cause

        raise last_error  # pragma: no cover — the loop always raises or returns

    @staticmethod
    def _assert_environment(env: Optional[str]) -> None:
        if env not in ("prod", "test"):
            raise ValueError(
                'env must be "prod" or "test" and must travel in the body. A "?env=test" query '
                "parameter only scopes searches: a create carrying just that produces a "
                "PRODUCTION transaction."
            )

    @staticmethod
    def _assert_redirect_urls(params: Mapping[str, Any]) -> None:
        """A server-to-server charge needs somewhere to send the payer afterwards.

        Without both URLs the platform answers ``201 Created`` with ``rejected``
        and ``return_url/fail_url params not specified`` — a real transaction,
        spent ``txid`` and all. Hosted payments and payouts do not need them.
        """
        hosted = params.get("hosted") is True or params.get("show_widget") is True
        if not hosted and params.get("money_storage") is not None:
            if not params.get("returnUrl") or not params.get("failUrl"):
                raise ValueError(
                    "A server-to-server payment needs both returnUrl and failUrl. Without them "
                    "the platform creates the transaction and immediately rejects it with "
                    '"return_url/fail_url params not specified".'
                )

    @staticmethod
    def _normalize_amount(params: Dict[str, Any]) -> Dict[str, Any]:
        """Accept ``"10.00"`` alongside ``"1000"``, converting with the currency's scale."""
        amount = params.get("amount")
        currency = params.get("currency", "")
        if isinstance(amount, str) and "." in amount:
            return {**params, "amount": to_minor_units(amount, currency)}
        if isinstance(amount, int):
            return {**params, "amount": str(amount)}
        return params


def _backoff(attempt: int, last_error: Optional[Exception]) -> float:
    """Exponential backoff with jitter; a rate limit's own ``Retry-After`` wins."""
    if isinstance(last_error, FourPayRateLimitError) and last_error.retry_after:
        return float(last_error.retry_after)
    return min(2 ** (attempt - 1), 8) + random.random() / 4
