"""Zoho settings: st.secrets in the app, environment / .env in scripts and GitHub Actions.

Nothing here logs or prints a secret value.
"""
from __future__ import annotations

import os

KEYS = ["ZOHO_CLIENT_ID", "ZOHO_CLIENT_SECRET", "ZOHO_REFRESH_TOKEN", "ZOHO_ORG_ID",
        "ZOHO_ACCOUNTS_URL", "ZOHO_API_BASE"]
DEFAULTS = {"ZOHO_ACCOUNTS_URL": "https://accounts.zoho.in",          # India data centre
            "ZOHO_API_BASE": "https://www.zohoapis.in/inventory/v1"}


def _get(key: str) -> str | None:
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    val = os.getenv(key) or None            # GitHub passes unset secrets as ""
    if val is None:
        try:
            import streamlit as st
            val = st.secrets.get(key) or None
        except Exception:                     # no secrets file outside Streamlit
            val = None
    if val is None:
        val = DEFAULTS.get(key)
    return None if val is None else str(val).strip()


def zoho_enabled() -> bool:
    """Feature flag. Default false: with it off the app behaves exactly as before."""
    return (_get("ZOHO_ENABLED") or "false").lower() in ("1", "true", "yes", "on")


def missing_keys() -> list[str]:
    return [k for k in KEYS if not _get(k)]


def settings() -> dict[str, str]:
    missing = missing_keys()
    if missing:
        from .zoho_client import ZohoError
        raise ZohoError(f"Zoho is enabled but these secrets are missing: {', '.join(missing)}. "
                        "See docs/zoho_setup.md.", code="setup", endpoint="config")
    return {k: _get(k) for k in KEYS}
