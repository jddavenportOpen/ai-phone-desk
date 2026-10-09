"""The gate: the only thing that can admit a dial.

An approval mints a single-use token bound to:
  - the request id, and
  - the sha256 of the EXACT bubble text the owner approved.
It expires after ``approval_ttl_s`` (16 h by default).

At dial time ``admit`` re-renders nothing itself: the caller passes the text it
is about to act on, and the gate checks, in order:
  1. the kill switch file is absent
  2. it is inside the calling window (owner's local time)
  3. today's dial count is under the daily cap
  4. the token exists, is unused, unexpired, for this request, and its hash
     matches the text. Any edit to the brief changes the text, so it changes
     the hash, so the token no longer admits anything.
A hash mismatch does NOT burn the token: the approval stands for what the owner
actually saw. A match consumes it, so one 👍 is one dial.

Every decision is appended to ledger.jsonl.

Honest limit: this is file state on one machine. It stops accidents, confused
agents and naive prompt injection. A process with write access to the state
directory could forge a token row, which is why the LoopMessage approver also
re-proves the 👍 against the vendor's own records at dial time.
"""
from __future__ import annotations

import secrets
from datetime import datetime
from typing import Optional

from .config import Config
from .store import Store

# Public AUTOMATED lines the line test may dial without approval: a recording, no
# person. NIST's WWV time announcement exists for the public to call.
TEST_LINES = {"+13034997111": "NIST WWV time announcement, Fort Collins CO (automated)"}


class GateDenied(RuntimeError):
    """``retryable`` = the same approval may still dial later (window, cap, kill switch)."""

    def __init__(self, reason: str, *, retryable: bool = False):
        super().__init__(reason)
        self.reason = reason
        self.retryable = retryable


class Gate:
    def __init__(self, cfg: Config, store: Store):
        self.cfg = cfg
        self.store = store

    # ── helpers ──────────────────────────────────────────────────────────────
    def local(self, now: Optional[datetime] = None) -> datetime:
        return (now or datetime.now(self.cfg.tz)).astimezone(self.cfg.tz)

    def in_window(self, now: Optional[datetime] = None) -> bool:
        h = self.local(now).hour
        return self.cfg.window_start <= h < self.cfg.window_end

    def dials_today(self, now: Optional[datetime] = None) -> int:
        day = self.local(now).strftime("%Y-%m-%d")
        return sum(1 for r in self.store.lines("ledger.jsonl")
                   if r.get("decision") == "ADMIT" and r.get("day") == day)

    def _audit(self, decision: str, now: datetime, **fields) -> None:
        self.store.append("ledger.jsonl", {"at": self.local(now).isoformat(timespec="seconds"),
                                           "day": self.local(now).strftime("%Y-%m-%d"),
                                           "decision": decision, **fields})

    # ── mint ─────────────────────────────────────────────────────────────────
    def mint(self, *, iid: str, text_sha: str, now: Optional[datetime] = None,
             evidence: Optional[dict] = None) -> str:
        """Called ONLY from the approval path, after the approver proved the 👍."""
        now = self.local(now)
        tok = secrets.token_hex(16)
        with self.store.locked():
            data = self.store.read("tokens.json", {"tokens": []})
            data["tokens"].append({"token": tok, "iid": iid, "sha": text_sha,
                                   "created_ts": now.timestamp(),
                                   "expires_ts": now.timestamp() + self.cfg.approval_ttl_s,
                                   "consumed_ts": None, "evidence": evidence or {}})
            data["tokens"] = data["tokens"][-500:]
            self.store.write("tokens.json", data)
        self._audit("MINT", now, iid=iid, sha=text_sha[:16])
        return tok

    def token_record(self, token: str) -> Optional[dict]:
        data = self.store.read("tokens.json", {"tokens": []})
        return next((t for t in data["tokens"] if t.get("token") == token), None)

    def token_expired(self, token: str, now: Optional[datetime] = None) -> bool:
        rec = self.token_record(token)
        return rec is None or self.local(now).timestamp() > float(rec.get("expires_ts") or 0)

    # ── admit ────────────────────────────────────────────────────────────────
    def admit(self, *, iid: str, text: str, token: Optional[str], to: str,
              now: Optional[datetime] = None) -> None:
        """Raise GateDenied, or consume the token and record ADMIT."""
        from .brief import content_hash
        now = self.local(now)
        if self.cfg.killed():
            self._audit("DENY", now, iid=iid, to=to, reason="kill switch")
            raise GateDenied(f"the phone desk is switched off ({self.cfg.kill_file.name})", retryable=True)
        if not self.in_window(now):
            self._audit("DENY", now, iid=iid, to=to, reason="outside window")
            raise GateDenied(f"outside calling hours ({self.cfg.window_start}:00-{self.cfg.window_end}:00 "
                             f"{self.cfg.owner_tz})", retryable=True)
        sha = content_hash(text)
        with self.store.locked():
            if self.dials_today(now) >= self.cfg.daily_cap:
                self._audit("DENY", now, iid=iid, to=to, reason="daily cap")
                raise GateDenied(f"today's cap of {self.cfg.daily_cap} calls is used up", retryable=True)
            data = self.store.read("tokens.json", {"tokens": []})
            rec = next((t for t in data["tokens"] if token and t.get("token") == token), None)
            reason = None
            if not token:
                reason = "no approval token"
            elif rec is None:
                reason = "approval token not recognized"
            elif rec.get("consumed_ts"):
                reason = "approval token already used (one 👍 is one call)"
            elif now.timestamp() > float(rec.get("expires_ts") or 0):
                reason = f"approval expired (older than {self.cfg.approval_ttl_s // 3600} h)"
            elif rec.get("iid") != iid:
                reason = "approval token belongs to a different request"
            elif rec.get("sha") != sha:
                reason = "the request changed after it was approved (text hash mismatch)"
            if reason:
                self._audit("DENY", now, iid=iid, to=to, reason=reason)
                raise GateDenied(reason)
            rec["consumed_ts"] = now.timestamp()
            self.store.write("tokens.json", data)
            self._audit("ADMIT", now, iid=iid, to=to, sha=sha[:16])

    def admit_test_line(self, *, to: str, now: Optional[datetime] = None) -> None:
        """The one dial that needs no approval: a public automated line. The kill
        switch and the daily cap still apply, and it is audited."""
        now = self.local(now)
        if to not in TEST_LINES:
            self._audit("DENY", now, iid="linetest", to=to, reason="not a public test line")
            raise GateDenied(f"{to} is not a public test line; refusing an unapproved dial")
        if self.cfg.killed():
            self._audit("DENY", now, iid="linetest", to=to, reason="kill switch")
            raise GateDenied(f"the phone desk is switched off ({self.cfg.kill_file.name})", retryable=True)
        with self.store.locked():
            if self.dials_today(now) >= self.cfg.daily_cap:
                self._audit("DENY", now, iid="linetest", to=to, reason="daily cap")
                raise GateDenied(f"today's cap of {self.cfg.daily_cap} calls is used up", retryable=True)
            self._audit("ADMIT", now, iid="linetest", to=to)
