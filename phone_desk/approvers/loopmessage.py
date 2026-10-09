"""The LoopMessage approver: the owner's 👍 tapback on the bubble in iMessage.

LoopMessage (loopmessage.com) is an iMessage API vendor. The desk sends the
bubble from your LoopMessage sender to the owner's phone. When the owner taps
👍, LoopMessage posts a ``message_reaction`` webhook to you.

A webhook payload alone is never trusted. Before an approval counts:
  1. the webhook carries the Authorization value you set in the LoopMessage
     dashboard (constant-time compare; no secret configured = refuse)
  2. LoopMessage's OWN webhook history holds that webhook id: a reaction, from
     the owner's contact, on that exact message id, placed (not removed),
     not in a group, under two hours old
  3. LoopMessage's own record of the reacted-to message shows the owner as the
     contact and the exact bubble text on record
  4. the webhook id was never used before (the desk records it as consumed)
At dial time (``reprove``) checks 2 and 3 run again, so a hand-edited state file
cannot place a call on its own.

Vendor facts this relies on (from LoopMessage's docs; re-check them against the
current docs before you depend on them):
  - send:   POST https://a.loopmessage.com/api/v1/message/send/  {contact, text, sender}
  - status: GET  https://a.loopmessage.com/api/v1/message/status/{message_id}/
  - history:GET  https://a.loopmessage.com/api/v1/webhooks/history/?page=&per_page=&sort_by=desc
  - auth:   one header, ``Authorization``, carrying the bare API key (no "Bearer")
  - a reaction webhook's ``message_id`` is the message reacted TO; the tapback
    arrives as ``reaction`` (older payloads) or ``reaction_type`` +
    ``reaction_event`` ("placed" | "removed")

Honest limit: this is not cryptographic. It stops accidents, agents and naive
prompt injection, which would have to forge the vendor's records to get through.
"""
from __future__ import annotations

import hmac
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from ..brief import content_hash
from . import NotApproved

SEND_URL = "https://a.loopmessage.com/api/v1/message/send/"
STATUS_URL = "https://a.loopmessage.com/api/v1/message/status/{message_id}/"
WEBHOOK_HISTORY_URL = "https://a.loopmessage.com/api/v1/webhooks/history/"
HISTORY_PAGES = 3
PROOF_MAX_AGE_S = 2 * 3600
MAX_LEN = 9_500                      # vendor cap is "under 10000 characters"
USER_AGENT = "ai-phone-desk/0.1"

DIAL_LIKES = frozenset({"like", "👍"})       # only a 👍 dials; a ❤️ is a reflex
DROPS = frozenset({"dislike", "👎"})
_TONES = re.compile("[\U0001F3FB-\U0001F3FF️]")

Http = Callable[[str, str, dict, Optional[dict], Optional[dict]], tuple[int, Any]]


def reaction_of(payload: dict) -> str:
    """The tapback in a reaction webhook as one string; '-like' if it was removed."""
    if not isinstance(payload, dict):
        return ""
    legacy = str(payload.get("reaction") or "").strip()
    if legacy:
        return legacy
    rtype = str(payload.get("reaction_type") or "").strip()
    if not rtype:
        return ""
    ev = str(payload.get("reaction_event") or "placed").strip().lower()
    return f"-{rtype}" if ev in ("removed", "remove", "deleted") else rtype


def plain(reaction: str) -> str:
    return _TONES.sub("", reaction or "").strip().lower()


def same_contact(a: str, b: str) -> bool:
    a, b = (a or "").strip(), (b or "").strip()
    if not a or not b:
        return False
    if "@" in a or "@" in b:
        return a.lower() == b.lower()
    da, db = re.sub(r"\D", "", a), re.sub(r"\D", "", b)
    return len(da) >= 10 and da[-10:] == db[-10:]


def _vendor_age_s(value, now_ts: float) -> float:
    try:
        dt = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        raise NotApproved("LoopMessage gave no time for that reaction") from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return now_ts - dt.timestamp()


def _urllib_http(method: str, url: str, headers: dict, params: Optional[dict], body: Optional[dict]):
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={**headers, "Content-Type": "application/json", "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read()
            status = r.status
    except urllib.error.HTTPError as exc:
        return exc.code, None
    try:
        return status, json.loads(raw) if raw else {}
    except ValueError:
        return status, None


class LoopMessageApprover:
    name = "loopmessage"

    def __init__(self, cfg, http: Optional[Http] = None, clock: Optional[Callable[[], float]] = None):
        self.cfg = cfg
        self.http = http or _urllib_http
        self.clock = clock or (lambda: datetime.now(timezone.utc).timestamp())

    # ── outbound ─────────────────────────────────────────────────────────────
    def _headers(self) -> dict:
        if not self.cfg.loopmessage_api_key:
            raise NotApproved("LOOPMESSAGE_API_KEY is not set")
        return {"Authorization": self.cfg.loopmessage_api_key}

    def _send_one(self, text: str) -> str:
        if not self.cfg.loopmessage_owner_contact:
            raise RuntimeError("LOOPMESSAGE_OWNER_CONTACT is not set")
        body = {"contact": self.cfg.loopmessage_owner_contact, "text": text}
        if self.cfg.loopmessage_sender:
            body["sender"] = self.cfg.loopmessage_sender
        status, data = self.http("POST", SEND_URL, self._headers(), None, body)
        if status >= 400 or not isinstance(data, dict) or data.get("success") is False:
            raise RuntimeError(f"LoopMessage send failed ({status}): {str(data)[:200]}")
        mid = str(data.get("message_id") or "")
        if not mid:
            raise RuntimeError("LoopMessage send returned no message_id")
        return mid

    def send(self, text: str, ref: str) -> list[str]:
        if len(text) > MAX_LEN:
            # A bubble is approved whole. Splitting it would mean a 👍 on half of it.
            raise RuntimeError("bubble is too long for one message; shorten the brief")
        return [self._send_one(text)]

    def notify(self, text: str) -> None:
        self._send_one(text[:MAX_LEN])

    # ── vendor records ───────────────────────────────────────────────────────
    def vendor_webhook(self, webhook_id: str) -> Optional[dict]:
        for page in range(1, HISTORY_PAGES + 1):
            status, data = self.http("GET", WEBHOOK_HISTORY_URL, self._headers(),
                                     {"page": page, "per_page": 50, "sort_by": "desc"}, None)
            if status >= 400 or not isinstance(data, dict) or not isinstance(data.get("items"), list):
                raise NotApproved(f"LoopMessage would not confirm the reaction ({status})")
            for item in data["items"]:
                if isinstance(item, dict) and str(item.get("webhook_id") or "") == webhook_id:
                    body = item.get("json")
                    if isinstance(body, str):
                        try:
                            body = json.loads(body)
                        except ValueError:
                            body = None
                    return {**item, "json": body if isinstance(body, dict) else {}}
            try:
                if page >= int(data.get("num_pages") or 1):
                    break
            except (TypeError, ValueError):
                break
        return None

    def vendor_status(self, message_id: str) -> Optional[dict]:
        status, data = self.http("GET", STATUS_URL.format(message_id=urllib.parse.quote(message_id, safe="")),
                                 self._headers(), None, None)
        if status == 404:
            return None
        if status >= 400 or not isinstance(data, dict):
            raise NotApproved(f"LoopMessage would not confirm the bubble ({status})")
        return data

    # ── proof ────────────────────────────────────────────────────────────────
    def webhook_authorized(self, headers: dict) -> bool:
        secret = self.cfg.loopmessage_webhook_secret
        got = next((v for k, v in (headers or {}).items() if k.lower() == "authorization"), "")
        return bool(secret) and hmac.compare_digest(str(got).encode(), secret.encode())

    def prove_reaction(self, payload: dict) -> dict:
        """Return {decision, message_id, webhook_id, reaction} for a reaction the
        vendor confirms came from the owner. decision is 'approve', 'skip', or
        'other' (a ❤️, a 😂, ...). Raises NotApproved."""
        webhook_id = str((payload or {}).get("webhook_id") or "").strip()
        if not re.fullmatch(r"[0-9A-Za-z-]{8,80}", webhook_id):
            raise NotApproved("that reaction carries no webhook id to confirm it by")
        claimed_mid = str(payload.get("message_id") or "")
        item = self.vendor_webhook(webhook_id)
        if item is None:
            raise NotApproved("LoopMessage has no record of that reaction")
        body = item.get("json") or {}
        reaction = reaction_of(body)
        if (str(item.get("event_type") or body.get("event") or "") != "message_reaction"
                or str(body.get("event") or "") != "message_reaction"
                or not same_contact(str(body.get("contact") or ""), self.cfg.loopmessage_owner_contact)
                or str(body.get("message_id") or "") != claimed_mid
                or body.get("group")):
            raise NotApproved("LoopMessage's record of that reaction does not match")
        if _vendor_age_s(item.get("create_date"), self.clock()) > PROOF_MAX_AGE_S:
            raise NotApproved("that reaction is too old to act on")
        p = plain(reaction)
        decision = "other" if p.startswith("-") else "approve" if p in DIAL_LIKES else "skip" if p in DROPS \
            else "other"
        return {"decision": decision, "message_id": claimed_mid, "webhook_id": webhook_id, "reaction": p}

    def check_bubble(self, message_id: str, text: str) -> None:
        rec = self.vendor_status(message_id)
        if rec is None:
            raise NotApproved("LoopMessage has no such bubble")
        if (not same_contact(str(rec.get("contact") or ""), self.cfg.loopmessage_owner_contact)
                or str(rec.get("text") or "").strip() != (text or "").strip()):
            raise NotApproved("the bubble you reacted to is not the request on record")

    def reprove(self, approval: dict, bubble_text: str, mids: list[str]) -> None:
        ev = approval.get("evidence") or {}
        mid, webhook_id = ev.get("message_id"), ev.get("webhook_id")
        if ev.get("kind") != "loopmessage" or not mid or not webhook_id or mid not in (mids or []):
            raise NotApproved("no recorded 👍 on this request's bubble")
        if ev.get("sha") != content_hash(bubble_text):
            raise NotApproved("the approved text is not the text on record")
        item = self.vendor_webhook(webhook_id)
        body = (item or {}).get("json") or {}
        if (item is None or str(body.get("event") or "") != "message_reaction"
                or str(body.get("message_id") or "") != mid
                or not same_contact(str(body.get("contact") or ""), self.cfg.loopmessage_owner_contact)
                or plain(reaction_of(body)) not in DIAL_LIKES):
            raise NotApproved("LoopMessage no longer shows your 👍 on that bubble")
        self.check_bubble(mid, bubble_text)
