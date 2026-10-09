"""The thin Vapi client, and the ONE place a phone call can be placed.

Vapi (api.vapi.ai) owns telephony, speech-to-text, text-to-speech and the
recording. This project owns the brief, the approval and the gate. Every dial
goes through ``place_call`` (gate.admit first) or ``place_test_call``
(gate.admit_test_line first). tests/test_one_door.py fails if the call-create
request appears anywhere else in the package.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any, Optional

from .config import Config
from .gate import Gate

API = "https://api.vapi.ai"
TIMEOUT_S = 30
# Vapi sits behind Cloudflare, which answers urllib's default User-Agent with a
# 403 (error 1010). Any explicit agent string gets through.
USER_AGENT = "ai-phone-desk/0.1"


class VapiError(RuntimeError):
    """``status`` is the HTTP code when Vapi answered, None when we never heard
    back (timeout, network). The second kind may have placed a call anyway."""

    def __init__(self, msg: str, status: Optional[int] = None):
        super().__init__(msg)
        self.status = status


class Vapi:
    def __init__(self, cfg: Config, opener=None):
        self.cfg = cfg
        self._open = opener or urllib.request.urlopen

    def request(self, method: str, path: str, body: Optional[dict] = None) -> Any:
        if not self.cfg.vapi_api_key:
            raise VapiError("VAPI_API_KEY is not set", status=400)
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"{API}{path}", data=data, method=method, headers={
            "Authorization": f"Bearer {self.cfg.vapi_api_key}", "Content-Type": "application/json",
            "User-Agent": USER_AGENT})
        try:
            with self._open(req, timeout=TIMEOUT_S) as r:
                raw = r.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:500]
            raise VapiError(f"{method} {path} -> HTTP {exc.code}: {detail}", exc.code) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise VapiError(f"{method} {path} -> {exc}") from None
        return json.loads(raw) if raw else None

    def get_call(self, call_id: str) -> dict:
        if not call_id:
            raise VapiError("no call id", status=400)
        return self.request("GET", f"/call/{call_id}") or {}

    def _ready(self) -> None:
        """Checked BEFORE the gate, so a missing key never burns an approval."""
        if not self.cfg.vapi_api_key:
            raise VapiError("VAPI_API_KEY is not set", status=400)
        if not self.cfg.vapi_phone_number_id:
            raise VapiError("VAPI_PHONE_NUMBER_ID is not set", status=400)

    def _create_call(self, to: str, assistant: dict) -> dict:
        return self.request("POST", "/call", {"phoneNumberId": self.cfg.vapi_phone_number_id,
                                              "customer": {"number": to}, "assistant": assistant})

    def place_call(self, gate: Gate, *, iid: str, text: str, token: Optional[str], to: str,
                   assistant: dict, now: Optional[datetime] = None) -> dict:
        """Dial ``to`` for request ``iid``. The gate consumes the approval token
        bound to ``text`` before anything leaves this machine; it raises
        GateDenied otherwise."""
        self._ready()
        gate.admit(iid=iid, text=text, token=token, to=to, now=now)
        return self._create_call(to, assistant)

    def place_test_call(self, gate: Gate, *, to: str, assistant: dict, now: Optional[datetime] = None) -> dict:
        self._ready()
        gate.admit_test_line(to=to, now=now)
        return self._create_call(to, assistant)
