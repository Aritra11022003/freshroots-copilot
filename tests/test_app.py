"""Smoke test: every page of app.py renders without exceptions (demo DB, no network)."""
import pytest
from streamlit.testing.v1 import AppTest

PAGES = ["Queues", "Data gaps", "Sign-off", "Backtest", "Export", "History", "Zoho"]


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("COPILOT_DEMO", "1")
    at = AppTest.from_file("../app.py", default_timeout=30)
    at.run()
    return at


def test_every_page_renders_with_banner(app):
    for p in PAGES:
        app.sidebar.radio[0].set_value(p).run()
        assert not app.exception, (p, app.exception)
        assert any("UNSIGNED ASSUMPTIONS" in e.value for e in app.error), p


def test_run_now_then_queues_show_all_skus(app):
    app.sidebar.button[0].click().run()
    assert not app.exception
    assert any("ok" in s.value for s in app.sidebar.success)
    app.sidebar.radio[0].set_value("Queues").run()
    assert "Approve queue (15)" in app.tabs[0].label
    assert "Exceptions (0)" in app.tabs[1].label


def test_zoho_page_off_shows_csv_fallback(app):
    app.sidebar.radio[0].set_value("Zoho").run()
    assert not app.exception
    assert any("switched off" in i.value for i in app.info)


def test_zoho_on_with_missing_secrets_shows_setup_message(monkeypatch):
    from copilot import zoho_config
    monkeypatch.setenv("COPILOT_DEMO", "1")
    monkeypatch.setenv("ZOHO_ENABLED", "true")
    monkeypatch.setattr(zoho_config, "missing_keys", lambda: ["ZOHO_REFRESH_TOKEN"])
    at = AppTest.from_file("../app.py", default_timeout=30)
    at.run()
    at.sidebar.radio[0].set_value("Zoho").run()
    assert not at.exception
    assert any("ZOHO_REFRESH_TOKEN" in e.value for e in at.error)
