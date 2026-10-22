"""Phase 1: Zoho client on a mocked HTTP layer (no live calls)."""
import pytest
import requests

from copilot.zoho_client import ZohoError
from tests.zoho_fakes import FakeResponse, FakeSession, client, fixture, ok


def test_token_fetched_and_org_id_and_timeout_on_every_request():
    s = FakeSession([ok(fixture("items_page2.json"))])
    z = client(s)
    z.get("items")
    c = s.calls[0]
    assert c["url"] == "https://www.zohoapis.in/inventory/v1/items"
    assert c["params"]["organization_id"] == "60012345"
    assert c["headers"]["Authorization"] == "Zoho-oauthtoken fake-access-1"
    assert c["timeout"] == 30 and s.token_calls == 1


def test_token_reused_until_expiry_then_refreshed():
    now = [1000.0]
    s = FakeSession([ok(fixture("items_page2.json"))] * 3)
    z = client(s, clock=lambda: now[0])
    z.get("items"); z.get("items")
    assert s.token_calls == 1
    now[0] += 3600                                   # an hour later
    z.get("items")
    assert s.token_calls == 2


def test_token_refresh_on_401_then_success():
    s = FakeSession([FakeResponse(401, fixture("invalid_token.json")), ok(fixture("items_page2.json"))],
                    tokens=[fixture("token.json"), dict(fixture("token.json"), access_token="fake-access-2")])
    z = client(s)
    assert z.get("items")["items"][0]["sku"] == "SKU003"
    assert s.token_calls == 2
    assert s.calls[1]["headers"]["Authorization"] == "Zoho-oauthtoken fake-access-2"


def test_second_401_raises_clear_error():
    s = FakeSession([FakeResponse(401, fixture("invalid_token.json"))] * 2)
    with pytest.raises(ZohoError, match="not authorized") as e:
        client(s).get("items")
    assert e.value.endpoint == "GET /items"


def test_pagination_reads_every_page():
    s = FakeSession([ok(fixture("items_page1.json")), ok(fixture("items_page2.json"))])
    items = client(s).list_items()
    assert [i["sku"] for i in items] == ["SKU001", "SKU002", "SKU003"]
    assert [c["params"]["page"] for c in s.calls] == [1, 2]
    assert all(c["params"]["per_page"] == 200 for c in s.calls)


def test_429_retries_with_backoff_then_succeeds():
    waits = []
    s = FakeSession([FakeResponse(429, fixture("rate_limited.json")),
                     FakeResponse(429, fixture("rate_limited.json")), ok(fixture("items_page2.json"))])
    z = client(s); z._sleep = waits.append
    assert z.get("items")["code"] == 0
    assert waits == [1, 2]


def test_5xx_retries_exhausted_raises():
    s = FakeSession([FakeResponse(503, {})] * 5)
    with pytest.raises(ZohoError, match="busy"):
        client(s).get("items")
    assert len(s.calls) == 5


def test_timeout_retried_then_clear_error():
    s = FakeSession([requests.Timeout()] * 5)
    with pytest.raises(ZohoError, match="30 seconds"):
        client(s).get("items")


def test_nonzero_code_raises_with_message_and_no_token_leak():
    s = FakeSession([FakeResponse(400, {"code": 1001, "message": "Item with SKU already exists"})])
    with pytest.raises(ZohoError) as e:
        client(s).post("items", {"name": "x"})
    assert "Item with SKU already exists" in str(e.value) and e.value.code == 1001
    assert "fake-access" not in str(e.value) and "refresh-xyz" not in str(e.value)


def test_login_failure_hints_at_india_domain():
    s = FakeSession(tokens=[{"error": "invalid_client"}])
    with pytest.raises(ZohoError, match="accounts.zoho.in"):
        client(s).get("items")


def test_client_exposes_no_issue_or_delete_operations():
    from copilot.zoho_client import ZohoClient
    banned = ("delete", "issue", "email", "receive", "bill", "approve", "cancel", "mark", "submit")
    assert not [m for m in dir(ZohoClient) if any(b in m.lower() for b in banned)]


def test_purchase_order_fixture_has_confirmed_quantity_fields():
    li = fixture("purchaseorder_detail.json")["purchaseorder"]["line_items"][0]
    assert {"item_id", "quantity", "quantity_received"} <= set(li)
