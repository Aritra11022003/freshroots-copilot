"""Small Zoho Inventory API client (requests).

Per Zoho's Inventory API v1 docs (checked 2026-10-22):
  - OAuth: POST {accounts}/oauth/v2/token, grant_type=refresh_token; access tokens last 1 hour;
    header "Authorization: Zoho-oauthtoken <token>".
  - Every request carries organization_id.
  - Lists paginate with page / per_page (max 200); responses carry page_context.has_more_page.
  - Body JSON has "code" (0 = success) and "message". Rate limit: HTTP 429 (codes 44/45/1070),
    100 requests/minute per org, 1,000/day on the free plan.

This client only exposes GET and POST. It is used for: list items/contacts/purchase orders,
get one purchase order, create item, create vendor contact, create DRAFT purchase order.
It never calls the status/issued, status/cancelled, submit, approve, email, receive, bill or
DELETE endpoints.
"""
from __future__ import annotations

import time

import requests

TIMEOUT = 30
MAX_RETRIES = 4
PER_PAGE = 200


class ZohoError(Exception):
    """One clear error the UI can show in plain words. Never contains a token."""

    def __init__(self, message: str, code=None, endpoint: str | None = None):
        super().__init__(message)
        self.message, self.code, self.endpoint = message, code, endpoint

    def __str__(self):
        where = f" [{self.endpoint}]" if self.endpoint else ""
        code = f" (code {self.code})" if self.code not in (None, "") else ""
        return f"{self.message}{code}{where}"


class ZohoClient:
    def __init__(self, client_id: str, client_secret: str, refresh_token: str, org_id: str,
                 accounts_url: str = "https://accounts.zoho.in",
                 api_base: str = "https://www.zohoapis.in/inventory/v1",
                 session: requests.Session | None = None, sleep=time.sleep, clock=time.time):
        self._cid, self._secret, self._refresh = client_id, client_secret, refresh_token
        self.org_id = str(org_id)
        self.accounts_url = accounts_url.rstrip("/")
        self.api_base = api_base.rstrip("/")
        self._http = session or requests.Session()
        self._sleep, self._clock = sleep, clock
        self._token: str | None = None
        self._expires_at = 0.0

    @classmethod
    def from_settings(cls, s: dict) -> "ZohoClient":
        return cls(s["ZOHO_CLIENT_ID"], s["ZOHO_CLIENT_SECRET"], s["ZOHO_REFRESH_TOKEN"],
                   s["ZOHO_ORG_ID"], s["ZOHO_ACCOUNTS_URL"], s["ZOHO_API_BASE"])

    # ---- auth ---------------------------------------------------------------------------
    def _access_token(self, force: bool = False) -> str:
        if force or not self._token or self._clock() >= self._expires_at - 60:
            endpoint = "oauth/v2/token"
            try:
                r = self._http.post(f"{self.accounts_url}/oauth/v2/token", timeout=TIMEOUT, params={
                    "grant_type": "refresh_token", "refresh_token": self._refresh,
                    "client_id": self._cid, "client_secret": self._secret})
                body = r.json()
            except (requests.RequestException, ValueError) as e:
                raise ZohoError(f"Could not reach Zoho login ({type(e).__name__})", endpoint=endpoint)
            if "access_token" not in body:
                err = body.get("error", "unknown_error")
                hint = (" Check ZOHO_ACCOUNTS_URL: India accounts must use https://accounts.zoho.in."
                        if err in ("invalid_client", "invalid_code") else "")
                raise ZohoError(f"Zoho login failed: {err}.{hint}", code=err, endpoint=endpoint)
            self._token = body["access_token"]
            self._expires_at = self._clock() + float(body.get("expires_in", 3600))
        return self._token

    # ---- core request -------------------------------------------------------------------
    def request(self, method: str, path: str, params: dict | None = None,
                json: dict | None = None) -> dict:
        endpoint = f"{method} /{path.lstrip('/')}"
        url = f"{self.api_base}/{path.lstrip('/')}"
        params = {**(params or {}), "organization_id": self.org_id}
        refreshed = False
        attempt = 0
        while True:
            headers = {"Authorization": f"Zoho-oauthtoken {self._access_token()}"}
            try:
                r = self._http.request(method, url, params=params, json=json, headers=headers,
                                       timeout=TIMEOUT)
            except requests.Timeout:
                err = ZohoError("Zoho did not answer within 30 seconds", code="timeout", endpoint=endpoint)
            except requests.RequestException as e:
                err = ZohoError(f"Could not reach Zoho ({type(e).__name__})", code="network", endpoint=endpoint)
            else:
                if r.status_code == 401 and not refreshed:          # expired / revoked token
                    self._access_token(force=True)
                    refreshed = True
                    continue
                if r.status_code == 429 or r.status_code >= 500:
                    err = ZohoError("Zoho is busy (rate limit or server error); please retry shortly",
                                    code=r.status_code, endpoint=endpoint)
                else:
                    try:
                        body = r.json()
                    except ValueError:
                        raise ZohoError(f"Zoho returned a non-JSON response (HTTP {r.status_code})",
                                        code=r.status_code, endpoint=endpoint)
                    if r.status_code >= 400 or body.get("code", 0) != 0:
                        raise ZohoError(body.get("message") or f"Zoho error (HTTP {r.status_code})",
                                        code=body.get("code", r.status_code), endpoint=endpoint)
                    return body
            if attempt >= MAX_RETRIES:
                raise err
            self._sleep(2 ** attempt)                                # 1, 2, 4, 8 s
            attempt += 1

    def get(self, path: str, params: dict | None = None) -> dict:
        return self.request("GET", path, params=params)

    def post(self, path: str, json: dict, params: dict | None = None) -> dict:
        return self.request("POST", path, params=params, json=json)

    def list_all(self, path: str, key: str, params: dict | None = None) -> list[dict]:
        """Every page of a list endpoint (page / per_page, until has_more_page is false)."""
        out, page = [], 1
        while True:
            body = self.get(path, {**(params or {}), "page": page, "per_page": PER_PAGE})
            out.extend(body.get(key, []))
            if not body.get("page_context", {}).get("has_more_page"):
                return out
            page += 1

    # ---- the only operations the copilot uses -------------------------------------------
    def list_items(self, **filters) -> list[dict]:
        return self.list_all("items", "items", filters)

    def list_contacts(self, **filters) -> list[dict]:
        return self.list_all("contacts", "contacts", filters)

    def list_purchase_orders(self, **filters) -> list[dict]:
        return self.list_all("purchaseorders", "purchaseorders", filters)

    def get_purchase_order(self, purchaseorder_id: str) -> dict:
        return self.get(f"purchaseorders/{purchaseorder_id}")["purchaseorder"]
