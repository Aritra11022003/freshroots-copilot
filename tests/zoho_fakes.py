"""Mocked HTTP layer for Zoho tests: queued fake responses, no network."""
import json
from pathlib import Path

import requests

FIX = Path(__file__).parent / "fixtures" / "zoho"


def fixture(name):
    return json.loads((FIX / name).read_text())


class FakeResponse:
    def __init__(self, status=200, body=None, raw=None):
        self.status_code, self._body, self._raw = status, body, raw

    def json(self):
        if self._raw is not None:
            raise ValueError("not json")
        return self._body


class FakeSession:
    """Answers token POSTs from `tokens` and API calls from `responses` (in order)."""

    def __init__(self, responses=(), tokens=None):
        self.responses = list(responses)
        self.tokens = list(tokens or [fixture("token.json")])
        self.calls, self.token_calls = [], 0

    def post(self, url, params=None, timeout=None):
        self.token_calls += 1
        body = self.tokens.pop(0) if len(self.tokens) > 1 else self.tokens[0]
        if isinstance(body, Exception):
            raise body
        return FakeResponse(200, body)

    def request(self, method, url, params=None, json=None, headers=None, timeout=None):
        self.calls.append({"method": method, "url": url, "params": dict(params or {}),
                           "json": json, "headers": dict(headers or {}), "timeout": timeout})
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def client(session, clock=None):
    from copilot.zoho_client import ZohoClient
    kw = {"clock": clock} if clock else {}
    return ZohoClient("cid", "csecret", "refresh-xyz", "60012345", session=session,
                      sleep=lambda s: None, **kw)


def ok(body):
    return FakeResponse(200, body)


__all__ = ["FakeResponse", "FakeSession", "client", "fixture", "ok", "requests"]
