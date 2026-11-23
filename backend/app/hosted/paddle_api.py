"""The few calls Mothli makes to Paddle's API (ADR 0045 sections 7 and 11, step 4).

Paddle's account is shared with another product of the owner's (MySigner), and an API key reaches
everything in the account: its customers and subscriptions too. So this client has no way to list
or search anything. Each method takes one id and acts on it alone, and the callers pass only ids
Mothli itself stored for the signed-in reader (``reader_plans``) or a price id from Mothli's own
settings:

- ``create_transaction``: ``POST /transactions`` with one of Mothli's prices, custom data
  naming the reader and their checkout, and Mothli's own page as ``checkout.url``
  (developer.paddle.com/api-reference/transactions/
  create-transaction; the browser opens checkout with its id:
  developer.paddle.com/paddlejs/methods/paddle-checkout-open);
- ``subscription``: ``GET /subscriptions/{id}`` (.../api-reference/subscriptions/get-subscription);
- ``cancel``: ``POST /subscriptions/{id}/cancel`` with ``effective_from: next_billing_period``
  (.../api-reference/subscriptions/cancel-subscription);
- ``remove_scheduled_change``: ``PATCH /subscriptions/{id}`` with ``scheduled_change: null``
  (developer.paddle.com/build/subscriptions/cancel-subscriptions);
- ``portal``: ``POST /customers/{id}/portal-sessions`` for one subscription
  (.../api-reference/customer-portals/create-customer-portal-session).

Every id is checked against Paddle's id format before it is put in a path. Requests carry the
key as a bearer token (developer.paddle.com/api-reference/about/authentication) and
``Paddle-Version: 1`` (.../api-reference/about/versioning), go to ``sandbox-api.paddle.com`` or
``api.paddle.com`` by ``PADDLE_ENVIRONMENT``, and time out quickly. Nothing is retried: a request
that may have reached Paddle is never sent twice, so no checkout or cancel can be made twice by
this client. Errors carry only a kind and, for a refusal, Paddle's error code
(developer.paddle.com/errors/overview); the logs hold one fixed line per failure and never the
key, a body, an id or an address.
"""

from __future__ import annotations

import logging
import re

import httpx

_LOG = logging.getLogger(__name__)
# httpx logs every request's address at INFO, and these addresses hold a subscription or customer
# id. The server's logs show warnings and above only; this keeps it so wherever logging is set up.
for _name in ("httpx", "httpcore"):
    if logging.getLogger(_name).level < logging.WARNING:
        logging.getLogger(_name).setLevel(logging.WARNING)

BASE_URLS = {"sandbox": "https://sandbox-api.paddle.com", "production": "https://api.paddle.com"}
TIMEOUT = httpx.Timeout(10.0, connect=3.0)
_SUBSCRIPTION = re.compile(r"sub_[a-z0-9]{26}")
_CUSTOMER = re.compile(r"ctm_[a-z0-9]{26}")
_PRICE = re.compile(r"pri_[a-z0-9]{26}")
_TRANSACTION = re.compile(r"txn_[a-z0-9]{26}")
_ERROR_CODE = re.compile(r"[a-z][a-z0-9_]{0,99}")
DOMAIN_NOT_APPROVED = "transaction_checkout_url_domain_is_not_approved"


class PaddleUnavailable(Exception):
    """Paddle did not answer, answered too slowly, was overloaded or answered something
    unreadable. Nothing is known to have changed at Paddle; nothing is written here."""

    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


class PaddleRefused(Exception):
    """Paddle answered and refused the request (a 4xx other than too many requests). ``code`` is
    Paddle's error code, for example ``subscription_locked_renewal``."""

    def __init__(self, status: int, code: str) -> None:
        super().__init__(code)
        self.status = status
        self.code = code


def _checked(value: str, pattern: re.Pattern) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ValueError("not a Paddle id of the expected kind")
    return value


class PaddleApi:
    def __init__(self, key: str, environment: str, *,
                 transport: httpx.BaseTransport | None = None) -> None:
        self._client = httpx.Client(
            base_url=BASE_URLS[environment], timeout=TIMEOUT, transport=transport,
            follow_redirects=False,
            headers={"Authorization": f"Bearer {key}", "Paddle-Version": "1",
                     "Accept": "application/json"})

    def close(self) -> None:
        self._client.close()

    def _call(self, method: str, path: str, body: dict | None = None) -> dict:
        try:
            response = self._client.request(method, path, json=body)
        except httpx.TimeoutException:
            _LOG.warning("paddle call failed: timeout")
            raise PaddleUnavailable("timeout") from None
        except httpx.HTTPError:
            _LOG.warning("paddle call failed: unreachable")
            raise PaddleUnavailable("unreachable") from None
        if response.status_code == 429 or response.status_code >= 500:
            _LOG.warning("paddle call failed: server")
            raise PaddleUnavailable("server")
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if 400 <= response.status_code < 500:
            error = payload.get("error") if isinstance(payload, dict) else None
            code = error.get("code") if isinstance(error, dict) else None
            _LOG.warning("paddle call refused")
            raise PaddleRefused(response.status_code, code if isinstance(code, str)
                                and _ERROR_CODE.fullmatch(code) else "unknown")
        data = payload.get("data") if isinstance(payload, dict) else None
        if not 200 <= response.status_code < 300 or not isinstance(data, dict):
            _LOG.warning("paddle call failed: unreadable")
            raise PaddleUnavailable("unreadable")
        return data

    def create_transaction(self, price_id: str, custom_data: dict[str, str],
                           checkout_url: str) -> str:
        """A transaction for one of Mothli's prices, quantity one, with ``custom_data``; its id.
        No customer is set: Paddle's checkout asks for (or is prefilled with) the email, and no
        customer is ever looked up or made here. ``checkout_url`` is Mothli's own page
        (``PADDLE_CHECKOUT_URL``): the account's default payment link belongs to another product,
        and a transaction's ``checkout.url`` overrides it for that transaction
        (developer.paddle.com/build/transactions/default-payment-link). Paddle refuses an address
        whose domain is not approved (``transaction_checkout_url_domain_is_not_approved``,
        developer.paddle.com/errors/transactions/transaction_checkout_url_domain_is_not_approved);
        the log then says what the operator must do."""
        if not isinstance(checkout_url, str) or not checkout_url.startswith("https://"):
            raise ValueError("not an https address")
        try:
            data = self._call("POST", "/transactions", {
                "items": [{"price_id": _checked(price_id, _PRICE), "quantity": 1}],
                "custom_data": custom_data,
                "checkout": {"url": checkout_url}})
        except PaddleRefused as refused:
            if refused.code == DOMAIN_NOT_APPROVED:
                _LOG.warning("paddle refused the checkout address: approve the domain of "
                             "PADDLE_CHECKOUT_URL in Paddle under Website approval")
            raise
        transaction = data.get("id")
        if not isinstance(transaction, str) or not _TRANSACTION.fullmatch(transaction):
            _LOG.warning("paddle call failed: unreadable")
            raise PaddleUnavailable("unreadable")
        return transaction

    def subscription(self, subscription_id: str) -> dict:
        return self._call("GET", f"/subscriptions/{_checked(subscription_id, _SUBSCRIPTION)}")

    def cancel(self, subscription_id: str) -> dict:
        """Cancel at the end of the period paid for; the subscription as Paddle now has it."""
        return self._call("POST", f"/subscriptions/{_checked(subscription_id, _SUBSCRIPTION)}"
                                  "/cancel", {"effective_from": "next_billing_period"})

    def remove_scheduled_change(self, subscription_id: str) -> dict:
        """Undo a scheduled cancel; the subscription as Paddle now has it."""
        return self._call("PATCH", f"/subscriptions/{_checked(subscription_id, _SUBSCRIPTION)}",
                          {"scheduled_change": None})

    def portal(self, customer_id: str, subscription_id: str) -> dict:
        """A customer portal session for one customer and one of their subscriptions: its
        ``urls``. The links carry a temporary token: never stored, never logged."""
        data = self._call("POST", f"/customers/{_checked(customer_id, _CUSTOMER)}/portal-sessions",
                          {"subscription_ids": [_checked(subscription_id, _SUBSCRIPTION)]})
        urls = data.get("urls")
        if not isinstance(urls, dict):
            _LOG.warning("paddle call failed: unreadable")
            raise PaddleUnavailable("unreadable")
        return urls
