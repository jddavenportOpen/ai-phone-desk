"""Shared fixtures. Nothing in the test suite may reach the network."""
from __future__ import annotations

import io
import json
import urllib.error
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from phone_desk import brief as B
from phone_desk.approvers import NotApproved
from phone_desk.config import Config
from phone_desk.desk import Desk
from phone_desk.vapi import Vapi

TZ = ZoneInfo("America/Chicago")
NOON = datetime(2026, 10, 6, 12, 0, tzinfo=TZ)
NIGHT = datetime(2026, 10, 6, 22, 0, tzinfo=TZ)
GYM = "+13125550147"     # 555-01XX numbers are reserved for fiction


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("a test tried to reach the network")
    monkeypatch.setattr(urllib.request, "urlopen", refuse)


class FakeApprover:
    name = "fake"

    def __init__(self):
        self.sent: list[tuple[str, str]] = []
        self.notes: list[str] = []
        self.reprove_error: str | None = None

    def send(self, text, ref):
        self.sent.append((text, ref))
        return [f"mid-{len(self.sent)}"]

    def notify(self, text):
        self.notes.append(text)

    def reprove(self, approval, bubble_text, mids):
        if self.reprove_error:
            raise NotApproved(self.reprove_error)


class FakeVapi:
    """Stands in for urllib.request.urlopen inside phone_desk.vapi.Vapi."""

    def __init__(self):
        self.requests: list[tuple[str, str, dict | None]] = []
        self.calls: dict[str, dict] = {}
        self.fail_post: Exception | None = None

    def posts(self):
        return [r for r in self.requests if r[0] == "POST" and r[1].endswith("/call")]

    def __call__(self, req, timeout=None):
        method, url = req.get_method(), req.full_url
        body = json.loads(req.data) if req.data else None
        self.requests.append((method, url, body))
        assert req.headers.get("User-agent", "").startswith("ai-phone-desk"), "Vapi needs an explicit User-Agent"
        if method == "POST" and url.endswith("/call"):
            if self.fail_post:
                raise self.fail_post
            cid = f"call-{len(self.posts())}"
            self.calls[cid] = {"id": cid, "status": "queued"}
            return _Resp({"id": cid})
        if method == "GET" and "/call/" in url:
            return _Resp(self.calls[url.rsplit("/", 1)[1]])
        raise AssertionError(f"unexpected Vapi request {method} {url}")


class _Resp(io.BytesIO):
    def __init__(self, obj):
        super().__init__(json.dumps(obj).encode())
        self.status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://api.vapi.ai/call", code, "err", {}, io.BytesIO(b'{"message":"bad"}'))


def make_cfg(tmp_path, **kw) -> Config:
    base = dict(owner_name="Alex Rivera", owner_tz="America/Chicago", state_dir=tmp_path / "state",
                vapi_api_key="test-key", vapi_phone_number_id="num-test")
    base.update(kw)
    return Config(**base)


@pytest.fixture
def cfg(tmp_path):
    return make_cfg(tmp_path)


@pytest.fixture
def approver():
    return FakeApprover()


@pytest.fixture
def fake_vapi():
    return FakeVapi()


@pytest.fixture
def desk(cfg, approver, fake_vapi):
    return Desk(cfg, approver=approver, vapi=Vapi(cfg, opener=fake_vapi))


def brief(**kw) -> B.Brief:
    d = dict(who="Northside Athletic Club", to="(312) 555-0147",
             goal="Cancel Alex Rivera's monthly membership effective today.",
             ask="Refund the most recent $49.00 monthly charge.",
             fallback="Ask exactly what Alex must do personally to cancel and get the refund.",
             facts=["Name on the account: Alex Rivera", "Member number: NAC-20417"])
    d.update(kw)
    return B.Brief(**d)


def approve(desk, iid, now=NOON, key=None):
    """What an approver does after proving the 👍."""
    return desk._record(iid, "approve", {"kind": "fake", "key": key or f"fake:{iid}"}, now)
