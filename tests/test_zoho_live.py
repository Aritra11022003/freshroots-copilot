"""Optional READ-ONLY live check against the Zoho demo org. Skipped unless ZOHO_LIVE_TESTS=1
and the Zoho secrets are set (normal test runs never call Zoho)."""
import os

import pytest

pytestmark = pytest.mark.skipif(os.getenv("ZOHO_LIVE_TESTS") != "1", reason="set ZOHO_LIVE_TESTS=1 to run")


def test_demo_org_reachable_and_mapping_complete():
    from copilot.zoho_client import ZohoClient
    from copilot.zoho_config import settings
    z = ZohoClient.from_settings(settings())
    skus = {i.get("sku") for i in z.list_items()}
    assert {f"SKU{i:03d}" for i in range(1, 16)} <= skus
    assert any(c.get("contact_name") == "Freshroots Supplier (demo)" for c in z.list_contacts())
    for po in z.list_purchase_orders():
        assert po["status"] == "draft"          # the copilot never issues anything
