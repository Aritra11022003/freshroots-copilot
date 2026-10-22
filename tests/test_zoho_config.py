"""Feature flag defaults off; missing secrets give a setup message, not a crash."""
import pytest

from copilot import zoho_config
from copilot.zoho_client import ZohoError


def test_flag_defaults_off(monkeypatch):
    monkeypatch.delenv("ZOHO_ENABLED", raising=False)
    monkeypatch.setattr(zoho_config, "_get", lambda k: zoho_config.DEFAULTS.get(k))
    assert zoho_config.zoho_enabled() is False


def test_missing_secrets_raise_setup_error(monkeypatch):
    monkeypatch.setattr(zoho_config, "_get", lambda k: {"ZOHO_ENABLED": "true"}.get(k, zoho_config.DEFAULTS.get(k)))
    with pytest.raises(ZohoError, match="ZOHO_CLIENT_ID, ZOHO_CLIENT_SECRET, ZOHO_REFRESH_TOKEN, ZOHO_ORG_ID"):
        zoho_config.settings()


def test_empty_env_value_counts_as_unset(monkeypatch):
    monkeypatch.setenv("ZOHO_ACCOUNTS_URL", "")
    assert zoho_config._get("ZOHO_ACCOUNTS_URL") == "https://accounts.zoho.in"
