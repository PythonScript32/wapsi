"""
Tests for the thin Razorpay wrapper. No network, no real client: _client() is
monkeypatched to a fake object that just records what create_payment_link
sent it.
"""
from __future__ import annotations

import pytest

from app.execution import razorpay_client as rc


class _FakePaymentLink:
    def __init__(self):
        self.calls = []

    def create(self, data, timeout=None):
        self.calls.append({"data": data, "timeout": timeout})
        return {"id": "plink_fake", "short_url": "https://rzp.io/i/fake", "status": "created"}


class _FakeClient:
    def __init__(self):
        self.payment_link = _FakePaymentLink()


@pytest.fixture
def fake_client(monkeypatch):
    client = _FakeClient()
    monkeypatch.setattr(rc, "_client", lambda: client)
    return client


# ---------------------------------------------------------------------------
# customer object — a dict, cleaned, never interpolated into text
# ---------------------------------------------------------------------------

def test_customer_payload_keeps_only_known_nonempty_fields():
    assert rc._customer_payload({"name": "Priya", "contact": "98765**43", "email": ""}) == {
        "name": "Priya", "contact": "98765**43",
    }
    assert rc._customer_payload({"name": None, "contact": "98765**43"}) == {"contact": "98765**43"}
    assert rc._customer_payload({}) == {}


def test_customer_payload_ignores_a_non_dict_input():
    assert rc._customer_payload("Priya Sharma") == {}
    assert rc._customer_payload(None) == {}


def test_create_payment_link_sends_a_proper_customer_object(fake_client):
    rc.create_payment_link(
        amount=499.0,
        customer={"name": "Priya Sharma", "contact": "98765**43"},
        idempotency_key="case_1:send_link:1",
    )
    data = fake_client.payment_link.calls[0]["data"]
    assert data["customer"] == {"name": "Priya Sharma", "contact": "98765**43"}


def test_create_payment_link_description_is_a_clean_string_not_a_dict_repr(fake_client):
    rc.create_payment_link(
        amount=499.0,
        customer={"name": "Priya Sharma", "contact": "98765**43"},
        idempotency_key="case_1:retry_after_date:1",
        purpose="subscription renewal",
    )
    data = fake_client.payment_link.calls[0]["data"]
    assert data["description"] == "Wapsi recovery - Rs 499 subscription renewal"
    assert "{" not in data["description"]
    assert "'name'" not in data["description"] and '"name"' not in data["description"]


def test_create_payment_link_description_defaults_purpose_when_not_given(fake_client):
    rc.create_payment_link(amount=1999.0, customer={}, idempotency_key="k")
    data = fake_client.payment_link.calls[0]["data"]
    assert data["description"] == "Wapsi recovery - Rs 1,999 payment"


# ---------------------------------------------------------------------------
# amount conversion, idempotency trail, timeout
# ---------------------------------------------------------------------------

def test_create_payment_link_amount_is_converted_to_paise(fake_client):
    rc.create_payment_link(amount=499.0, customer={}, idempotency_key="k")
    assert fake_client.payment_link.calls[0]["data"]["amount"] == 49900


def test_create_payment_link_stashes_the_idempotency_key_for_traceability(fake_client):
    rc.create_payment_link(amount=499.0, customer={}, idempotency_key="case_1:send_link:1")
    data = fake_client.payment_link.calls[0]["data"]
    assert data["reference_id"] == "case_1:send_link:1"
    assert data["notes"]["idempotency_key"] == "case_1:send_link:1"


def test_create_payment_link_uses_a_10s_timeout(fake_client):
    rc.create_payment_link(amount=499.0, customer={}, idempotency_key="k")
    assert fake_client.payment_link.calls[0]["timeout"] == 10


# ---------------------------------------------------------------------------
# fetch_payment / fetch_order share the same client and timeout
# ---------------------------------------------------------------------------

def test_fetch_payment_and_fetch_order_use_the_shared_client(monkeypatch):
    calls = {}

    class _FakePayment:
        def fetch(self, payment_id, timeout=None):
            calls["payment"] = (payment_id, timeout)
            return {"id": payment_id}

    class _FakeOrder:
        def fetch(self, order_id, timeout=None):
            calls["order"] = (order_id, timeout)
            return {"id": order_id}

    class _Client:
        payment = _FakePayment()
        order = _FakeOrder()

    monkeypatch.setattr(rc, "_client", lambda: _Client())

    assert rc.fetch_payment("pay_123") == {"id": "pay_123"}
    assert rc.fetch_order("order_123") == {"id": "order_123"}
    assert calls["payment"] == ("pay_123", 10)
    assert calls["order"] == ("order_123", 10)


# ---------------------------------------------------------------------------
# missing credentials fail loudly, never silently
# ---------------------------------------------------------------------------

def test_client_raises_loudly_without_credentials(monkeypatch):
    rc._client.cache_clear()
    monkeypatch.setattr(rc.config, "RAZORPAY_KEY_ID", "")
    monkeypatch.setattr(rc.config, "RAZORPAY_KEY_SECRET", "")
    with pytest.raises(RuntimeError):
        rc._client()
    rc._client.cache_clear()
