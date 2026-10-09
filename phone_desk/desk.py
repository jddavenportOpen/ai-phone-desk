"""The phone desk: an agent asks for a call; the owner's 👍 places it.

THE LOOP
  1. PROPOSE  an agent (or you, or an external agent's CALL REQUEST) writes a
              brief: who, number, goal, the ask, the fallback, and the ONLY facts
              the voice agent may share.
  2. ASK      one bubble to the owner showing every field. The voice agent's
              instructions are rendered from the same fields (brief.py), so the
              bubble IS the whole brief.
  3. APPROVE  the owner's 👍 on that exact bubble, proven by the approver, mints
              a single-use token bound to the sha256 of the bubble text (16 h).
              👎 drops it. Nothing else approves a call.
  4. DIAL     the tick dials queued calls inside the calling window: re-prove the
              approval, then vapi.place_call -> gate.admit (kill switch, window,
              daily cap, token) -> POST /call.
  5. REPORT   the tick polls the call, writes outcome + transcript under
              <state>/calls/, and notifies the owner.

Statuses: draft -> surfaced -> queued -> dialing -> in-call -> done
          (or skipped | expired | failed | unknown). ``unknown`` means the dial
          may or may not have gone out; nothing ever redials it automatically.
"""
from __future__ import annotations

import json
import logging
import re
import secrets
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from . import brief as B
from .approvers import AlreadyUsed, Approver, NotApproved
from .config import Config
from .gate import TEST_LINES, Gate, GateDenied
from .store import Store
from .vapi import Vapi, VapiError

log = logging.getLogger("phone_desk")

STUCK_DIAL_S = 5 * 60          # a dial that never recorded a call id
STUCK_CALL_S = 45 * 60         # Vapi never reported an end: stop polling
ANALYSIS_GRACE_S = 90          # the structured result lands a few seconds after the end

_NO_TALK = {"customer-did-not-answer": "no answer", "customer-busy": "busy", "voicemail": "voicemail",
            "twilio-failed-to-connect-call": "the call failed to connect"}


class Desk:
    def __init__(self, cfg: Config, *, store: Optional[Store] = None, approver: Optional[Approver] = None,
                 vapi: Optional[Vapi] = None):
        self.cfg = cfg
        self.store = store or Store(cfg.state_dir)
        if approver is None:
            from .approvers import build
            approver = build(cfg)
        self.approver = approver
        self.vapi = vapi or Vapi(cfg)
        self.gate = Gate(cfg, self.store)
        self._setup_log()

    def _setup_log(self) -> None:
        path = str(self.store.path("desk.log"))
        if not any(getattr(h, "baseFilename", None) == path for h in log.handlers):
            h = logging.FileHandler(path)
            h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            log.addHandler(h)
        log.setLevel(logging.INFO)

    def now(self, now: Optional[datetime] = None) -> datetime:
        return (now or datetime.now(self.cfg.tz)).astimezone(self.cfg.tz)

    def render(self, item: dict) -> str:
        return B.render(B.Brief.from_dict(item["brief"]), item["id"], self.cfg.owner_name)

    def _notify(self, text: str) -> None:
        try:
            self.approver.notify(text)
        except Exception as exc:  # noqa: BLE001 -- the record on disk still holds it
            log.warning("could not notify the owner: %s", exc)

    def _update(self, iid: str, **fields) -> dict:
        with self.store.locked():
            data = self.store.load_desk()
            it = data["items"][iid]
            it.update(updated_at=self.now().isoformat(timespec="seconds"), **fields)
            self.store.save_desk(data)
            return it

    def get(self, iid: str) -> Optional[dict]:
        return self.store.load_desk()["items"].get(iid)

    # ── 1-2. propose / surface ───────────────────────────────────────────────
    def propose(self, b: B.Brief, *, hold: bool = False, dry_run: bool = False,
                now: Optional[datetime] = None) -> dict:
        """Record a brief. ``hold`` keeps it as a draft (surface it later);
        otherwise its bubble goes to the owner now."""
        now = self.now(now)
        iid = secrets.token_hex(4)
        item = {"id": iid, "status": "draft" if hold else "proposed", "created_at": now.isoformat(timespec="seconds"),
                "brief": b.to_dict(), "bubble": {"text": B.render(b, iid, self.cfg.owner_name), "mids": [],
                                                 "at_ts": None}}
        if dry_run:
            return item
        with self.store.locked():
            data = self.store.load_desk()
            data["items"][iid] = item
            self.store.save_desk(data)
        log.info("proposed %s to %s (%s) origin=%s hold=%s", iid, b.who, b.to, b.origin, hold)
        return item if hold else self.surface(iid, now=now)

    def surface(self, iid: str, *, now: Optional[datetime] = None) -> dict:
        now = self.now(now)
        it = self.get(iid)
        if not it or it.get("status") not in ("proposed", "draft"):
            raise RuntimeError(f"{iid} is not a request waiting to be shown to the owner")
        text = it["bubble"]["text"]
        if self.render(it) != text:
            raise RuntimeError(f"{iid}'s brief no longer renders to its bubble; propose a new request")
        mids = self.approver.send(text, iid)
        log.info("surfaced %s via %s", iid, self.approver.name)
        return self._update(iid, status="surfaced", bubble={"text": text, "mids": mids, "at_ts": now.timestamp()},
                            approver=self.approver.name)

    # ── 3. approval ──────────────────────────────────────────────────────────
    def _consume_evidence(self, key: str) -> None:
        with self.store.locked():
            used = self.store.read("consumed.json", {"ids": []})["ids"]
            if key in used:
                raise AlreadyUsed("that approval was already acted on")
            used.append(key)
            self.store.write("consumed.json", {"ids": used[-1000:]})

    def _record(self, iid: str, decision: str, evidence: dict, now: datetime) -> str:
        """Shared by every approver, AFTER it proved the gesture. Mints the token."""
        with self.store.locked():
            data = self.store.load_desk()
            it = data["items"].get(iid)
            if it is None:
                return "That isn't one of my call requests, so I did nothing."
            who = it["brief"]["who"]
            st = it.get("status")
            if st != "surfaced":
                return {"skipped": "Already dropped.", "expired": "That request expired, so I dialed nothing.",
                        "queued": f"Already approved; the call to {who} is queued.",
                        "dialing": f"Already calling {who}.", "in-call": f"Already on the phone with {who}.",
                        "done": f"Already called {who}."}.get(st, "That request isn't waiting on you.")
            text = it["bubble"]["text"]
            if now.timestamp() - float(it["bubble"].get("at_ts") or 0) > self.cfg.bubble_max_age_s:
                it.update(status="expired", updated_at=now.isoformat(timespec="seconds"))
                self.store.save_desk(data)
                return "That request is over a day old, so I dialed nothing. Propose a fresh one."
            if self.render(it) != text:
                return "That request no longer matches what you saw, so I dialed nothing."
            try:
                self._consume_evidence(evidence["key"])
            except AlreadyUsed as exc:
                return f"Not done: {exc}."
            if decision == "skip":
                it.update(status="skipped", updated_at=now.isoformat(timespec="seconds"))
                self.store.save_desk(data)
                log.info("dropped %s", iid)
                return f"Dropped. I won't call {who}."
            evidence = {**evidence, "sha": B.content_hash(text)}
            token = self.gate.mint(iid=iid, text_sha=evidence["sha"], now=now, evidence=evidence)
            it.update(status="queued", updated_at=now.isoformat(timespec="seconds"),
                      approval={"by": self.approver.name, "evidence": evidence, "token": token,
                                "approved_at": now.isoformat(timespec="seconds"), "approved_ts": now.timestamp()})
            self.store.save_desk(data)
        log.info("approved %s via %s", iid, self.approver.name)
        if self.cfg.killed():
            return f"Approved, but the phone desk is switched off, so the call to {who} is on hold."
        if not self.gate.in_window(now):
            return (f"Approved. It's outside calling hours, so I'll call {who} after "
                    f"{self.cfg.window_start}:00 and tell you how it went.")
        return f"📞 Approved. Calling {who} on the next tick; I'll tell you how it went."

    def approve_terminal(self, iid: str, *, now: Optional[datetime] = None) -> str:
        from .approvers.terminal import TerminalApprover
        if not isinstance(self.approver, TerminalApprover):
            return "This desk uses the LoopMessage approver: 👍 the bubble in iMessage instead."
        now = self.now(now)
        it = self.get(iid)
        if not it or it.get("status") != "surfaced":
            return "That request isn't waiting on you."
        try:
            decision = self.approver.ask(it["bubble"]["text"], iid)
        except NotApproved as exc:
            return f"Not done: {exc}."
        if decision is None:
            return "Cancelled; nothing changed."
        return self._record(iid, decision, {"kind": "terminal", "key": f"terminal:{iid}:{secrets.token_hex(8)}"},
                            now)

    def handle_webhook(self, payload: dict, headers: dict, *, now: Optional[datetime] = None) -> dict:
        """A LoopMessage webhook. Reactions on our bubbles become decisions;
        messages from the configured external agent go to external.ingest.
        Everything else is ignored. Never raises on bad input."""
        from .approvers.loopmessage import LoopMessageApprover, plain, reaction_of, same_contact
        now = self.now(now)
        ap = self.approver
        if not isinstance(ap, LoopMessageApprover):
            return {"ok": False, "reason": "this desk is not using the LoopMessage approver"}
        if not ap.webhook_authorized(headers):
            return {"ok": False, "reason": "unauthorized webhook"}
        if not isinstance(payload, dict):
            return {"ok": False, "reason": "malformed"}
        event = str(payload.get("event") or "").lower()
        if event == "message_inbound":
            contact = payload.get("contact")
            if (isinstance(contact, str) and self.cfg.external_agent_contact
                    and same_contact(contact, self.cfg.external_agent_contact)):
                from . import external
                return {"ok": True, "external": external.ingest(self, str(payload.get("text") or ""),
                                                                agent=self.cfg.external_agent_name, now=now)}
            return {"ok": True, "ignored": "inbound message not from the external agent"}
        if event != "message_reaction":
            return {"ok": True, "ignored": event or "no event"}
        mid = str(payload.get("message_id") or "")
        data = self.store.load_desk()
        iid = next((i for i, it in data["items"].items() if mid and mid in (it.get("bubble") or {}).get("mids", [])),
                   None)
        if iid is None:
            return {"ok": True, "ignored": "reaction is not on a call request"}
        if plain(reaction_of(payload)).startswith("-"):
            return {"ok": True, "ignored": "reaction removed"}
        try:
            proof = ap.prove_reaction(payload)
            if proof["decision"] == "other":
                reply = "Only a 👍 places a call (a ❤️ doesn't). Nothing dialed."
            else:
                if proof["decision"] == "approve":
                    ap.check_bubble(mid, data["items"][iid]["bubble"]["text"])
                reply = self._record(iid, proof["decision"], {"kind": "loopmessage", "key": f"lm:{proof['webhook_id']}",
                                                              "message_id": mid, "webhook_id": proof["webhook_id"],
                                                              "reaction": proof["reaction"]}, now)
        except NotApproved as exc:
            reply = f"Not done: {exc}."
        self._notify(reply)
        return {"ok": True, "id": iid, "reply": reply}

    # ── 4. dial ──────────────────────────────────────────────────────────────
    def dial(self, iid: str, *, now: Optional[datetime] = None) -> str:
        now = self.now(now)
        with self.store.locked():
            data = self.store.load_desk()
            it = data["items"].get(iid)
            if not it or it.get("status") != "queued":
                return "That call isn't approved, so I dialed nothing."
            b = B.Brief.from_dict(it["brief"])
            if self.cfg.killed():
                return "The phone desk is switched off; holding the call."
            if not self.gate.in_window(now):
                return f"Outside calling hours; the call to {b.who} waits for the window."
            text = it["bubble"]["text"]
            if self.render(it) != text:
                it.update(status="failed", last_error="brief changed after approval")
                self.store.save_desk(data)
                return "The request changed after you approved it, so I dialed nothing."
            approval = it.get("approval") or {}
            if self.gate.token_expired(approval.get("token", ""), now):
                it.update(status="expired", last_error="approval expired before a dial")
                self.store.save_desk(data)
                return f"Your approval for {b.who} expired before I could dial. Nothing was dialed."
            it.update(status="dialing", dialing_ts=now.timestamp())
            self.store.save_desk(data)
        try:
            self.approver.reprove(approval, text, it["bubble"].get("mids") or [])
        except NotApproved as exc:
            self._update(iid, status="surfaced", approval=None, last_error=f"reprove: {exc}")
            log.warning("dial %s: approval not re-proven: %s", iid, exc)
            return f"Didn't call {b.who}: {exc}. 👍 the request again to dial it."
        try:
            call = self.vapi.place_call(self.gate, iid=iid, text=text, token=approval.get("token"), to=b.to,
                                        assistant=self._assistant(b), now=now)
        except GateDenied as exc:
            if exc.retryable:
                self._update(iid, status="queued", last_error=exc.reason)
                return f"Holding the call to {b.who}: {exc.reason}."
            self._update(iid, status="failed", last_error=exc.reason)
            return f"Didn't call {b.who}: {exc.reason}. Nothing was dialed."
        except Exception as exc:  # noqa: BLE001
            # A 4xx from Vapi happens before any call. No answer at all (timeout,
            # network, 5xx) may still have dialed: never claim otherwise.
            status = exc.status if isinstance(exc, VapiError) else None
            if status is not None and 400 <= int(status) < 500:
                self._update(iid, status="failed", last_error=str(exc)[:500])
                return f"Couldn't place the call to {b.who}: {str(exc)[:200]}. Nothing was dialed."
            self._update(iid, status="unknown", last_error=str(exc)[:500])
            log.warning("dial %s unknown: %s", iid, exc)
            return (f"⚠️ Not sure the call to {b.who} went out ({str(exc)[:120]}). I won't redial on my own; "
                    f"check your Vapi dashboard before asking again.")
        self._update(iid, status="in-call", call={"id": (call or {}).get("id"), "placed_ts": now.timestamp()})
        log.info("dialed %s -> %s call=%s", iid, b.to, (call or {}).get("id"))
        return f"📞 Calling {b.who} now. I'll tell you how it went."

    def _assistant(self, b: B.Brief) -> dict:
        c = self.cfg
        return B.assistant(b, c.owner_name, provider=c.vapi_model_provider, model=c.vapi_model,
                           voice_provider=c.vapi_voice_provider, voice_id=c.vapi_voice_id)

    # ── 5. report ────────────────────────────────────────────────────────────
    @staticmethod
    def artifact(call: dict) -> dict:
        art = call.get("artifact") or {}
        ana = call.get("analysis") or {}
        secs = None
        try:
            secs = int((datetime.fromisoformat(call["endedAt"].replace("Z", "+00:00"))
                        - datetime.fromisoformat(call["startedAt"].replace("Z", "+00:00"))).total_seconds())
        except (KeyError, TypeError, ValueError, AttributeError):
            pass
        return {"ended_reason": call.get("endedReason"), "seconds": secs, "cost": call.get("cost"),
                "transcript": art.get("transcript") or call.get("transcript") or "",
                "recording_url": art.get("recordingUrl") or call.get("recordingUrl"),
                "summary": ana.get("summary") or call.get("summary") or "",
                "data": ana.get("structuredData") or {}}

    @staticmethod
    def result_text(b: B.Brief, r: dict, *, rehearsal: bool = False) -> str:
        d = r.get("data") or {}
        head = ("🎭 Rehearsal" if rehearsal else "📞") + f" {b.who}: "
        reason = r.get("ended_reason") or ""
        if not r.get("transcript") and reason in _NO_TALK:
            return head + f"{_NO_TALK[reason]}. Nothing was said."
        mins = f" ({max(1, round((r.get('seconds') or 0) / 60))} min)" if r.get("seconds") else ""
        lines = [head + (d.get("outcome") or "ended").replace("_", " ") + mins]
        for label, key in (("Did", "what_they_did"), ("Confirmation", "confirmation"), ("Spoke with", "staff_name"),
                           ("Money", "money"), ("Left for you", "next_step_for_owner"), ("Check by", "follow_up_by")):
            v = str(d.get(key) or "").strip()
            if v and v.lower() not in ("none", "n/a", "empty", "no"):
                lines.append(f"- {label}: {v}")
        if len(lines) == 1 and r.get("summary"):
            lines.append(str(r["summary"])[:500])
        return "\n".join(lines)

    def write_log(self, iid: str, b: B.Brief, r: dict, *, kind: str = "call") -> Path:
        d = self.store.path("calls")
        d.mkdir(parents=True, exist_ok=True)
        stem = f"{self.now():%Y-%m-%d}-{kind}-{iid}"
        data = r.get("data") or {}
        body = [f"# {kind.title()} · {b.who} · {self.now():%a %b %d, %H:%M}", "",
                f"- Number: {B.pretty(b.to)}", f"- Goal: {b.goal}", f"- Outcome: {data.get('outcome', '?')}",
                f"- Ended: {r.get('ended_reason')} after {r.get('seconds')} s · cost ${r.get('cost')}",
                f"- Recording: {r.get('recording_url')}", "", "## Structured result", "```json",
                json.dumps(data, indent=1, ensure_ascii=False), "```", "", "## Summary", r.get("summary") or "", "",
                "## Transcript", r.get("transcript") or "(none)", ""]
        (d / f"{stem}.md").write_text("\n".join(body))
        (d / f"{stem}.json").write_text(json.dumps({"id": iid, "brief": b.to_dict(), **r}, indent=1,
                                                   ensure_ascii=False, default=str))
        return d / f"{stem}.md"

    def finalize(self, iid: str, call: dict) -> None:
        r = self.artifact(call)
        with self.store.locked():                # claim it first: a second tick finds "done" and stops
            data = self.store.load_desk()
            it = data["items"][iid]
            if it.get("status") != "in-call":
                return
            it.update(status="done", result={k: v for k, v in r.items() if k != "transcript"},
                      ended_at=self.now().isoformat(timespec="seconds"))
            self.store.save_desk(data)
        b = B.Brief.from_dict(it["brief"])
        path = self.write_log(iid, b, r)
        self._update(iid, log=str(path))
        self._notify(self.result_text(b, r) + f"\nTranscript: {path.name}")
        log.info("finalized %s outcome=%s", iid, (r.get("data") or {}).get("outcome"))

    # ── the tick (run it every minute: cron, launchd, systemd) ───────────────
    def tick(self, *, now: Optional[datetime] = None) -> dict:
        now = self.now(now)
        out = {"dialed": 0, "finalized": 0, "expired": 0, "unknown": 0, "errors": 0}
        for iid, it in list(self.store.load_desk()["items"].items()):
            try:
                self._tick_item(iid, it, now, out)
            except Exception as exc:  # noqa: BLE001 -- one bad item must not stall the rest
                out["errors"] += 1
                log.warning("tick %s: %s", iid, exc)
        return out

    def _tick_item(self, iid: str, it: dict, now: datetime, out: dict) -> None:
        st = it.get("status")
        who = it["brief"]["who"]
        ts = now.timestamp()
        if st == "surfaced" and ts - float((it.get("bubble") or {}).get("at_ts") or 0) > self.cfg.bubble_max_age_s:
            self._update(iid, status="expired")
            out["expired"] += 1
        elif st == "queued":
            if self.gate.token_expired((it.get("approval") or {}).get("token", ""), now):
                self._update(iid, status="expired", last_error="approval expired before a dial")
                self._notify(f"Your approval to call {who} expired before calling hours. Nothing was dialed.")
                out["expired"] += 1
            elif self.gate.in_window(now) and not self.cfg.killed():
                day = now.strftime("%Y-%m-%d")
                if self.gate.dials_today(now) >= self.cfg.daily_cap:
                    if it.get("cap_noted") != day:          # say it once, not every minute
                        self._update(iid, cap_noted=day)
                        self._notify(f"Today's cap of {self.cfg.daily_cap} calls is used up; the call to {who} "
                                     f"waits (your approval lapses after "
                                     f"{self.cfg.approval_ttl_s // 3600} h).")
                    return
                self._notify(self.dial(iid, now=now))
                out["dialed"] += 1
        elif st == "dialing" and ts - float(it.get("dialing_ts") or 0) > STUCK_DIAL_S:
            self._update(iid, status="unknown", last_error="dial never recorded a call id")
            self._notify(f"⚠️ I can't tell whether the call to {who} went out. I won't redial on my own; "
                         f"check your Vapi dashboard.")
            out["unknown"] += 1
        elif st == "in-call":
            call = (it.get("call") or {})
            c = self.vapi.get_call(call.get("id") or "")
            if self._ended(c, ts):
                self.finalize(iid, c)
                out["finalized"] += 1
            elif ts - float(call.get("placed_ts") or ts) > STUCK_CALL_S:
                self._update(iid, status="unknown", last_error="Vapi never reported the call ending")
                self._notify(f"⚠️ The call to {who} never reported an end. Check the Vapi dashboard; "
                             f"I won't redial on my own.")
                out["unknown"] += 1

    @staticmethod
    def _ended(c: dict, now_ts: float) -> bool:
        if c.get("status") != "ended":
            return False
        if (c.get("analysis") or {}).get("structuredData") or c.get("endedReason") in _NO_TALK:
            return True
        try:
            ended = datetime.fromisoformat(str(c.get("endedAt")).replace("Z", "+00:00")).timestamp()
        except ValueError:
            return True
        return now_ts - ended > ANALYSIS_GRACE_S

    # ── the line test: the one call that needs no approval ───────────────────
    LINETEST_PROMPT = ("This is an automated line test of a phone system. You will hear a recorded time "
                       "announcement. Say nothing. After you have heard the time announced once, end the call "
                       "with the endCall tool.")

    def linetest(self, *, wait_s: int = 150, sleep: Callable[[float], None] = time.sleep) -> dict:
        """Dial NIST's public time line and check the whole pipe: dial, audio,
        transcript, end. Catches a dead number or an empty Vapi balance before an
        approved call fails silently."""
        to = next(iter(TEST_LINES))
        res = {"at": self.now().isoformat(timespec="seconds"), "to": to, "ok": False}
        c = self.cfg
        assistant = {"name": "ai-phone-desk-linetest", "firstMessageMode": "assistant-waits-for-user",
                     "model": {"provider": c.vapi_model_provider, "model": c.vapi_model, "temperature": 0,
                               "messages": [{"role": "system", "content": self.LINETEST_PROMPT}],
                               "tools": [{"type": "endCall"}]},
                     "voice": {"provider": c.vapi_voice_provider, "voiceId": c.vapi_voice_id},
                     "transcriber": {"provider": "deepgram", "model": "nova-3", "language": "en"},
                     # NIST speaks the time once a minute between ticks
                     "maxDurationSeconds": 85, "silenceTimeoutSeconds": 80,
                     "artifactPlan": {"recordingEnabled": False}}
        try:
            call = self.vapi.place_test_call(self.gate, to=to, assistant=assistant)
            res["call_id"] = call.get("id")
            deadline = time.time() + wait_s
            cur = call
            while time.time() < deadline:
                sleep(8)
                cur = self.vapi.get_call(call["id"])
                if cur.get("status") == "ended":
                    break
            r = self.artifact(cur)
            heard = (r.get("transcript") or "").lower()
            res.update(status=cur.get("status"), ended_reason=r.get("ended_reason"), seconds=r.get("seconds"),
                       cost=r.get("cost"), heard=heard[:200])
            res["ok"] = bool(cur.get("status") == "ended" and (r.get("seconds") or 0) >= 3
                             and any(w in heard for w in ("coordinated", "universal", "time", "hours", "minutes")))
        except Exception as exc:  # noqa: BLE001 -- the failure IS the finding
            res["error"] = str(exc)[:400]
        self.store.write("linetest.json", res)
        log.info("linetest ok=%s", res["ok"])
        return res

    # ── rehearsal (text) ─────────────────────────────────────────────────────
    TEXT_SIM_NOTE = ("\n\nTHIS IS A TEXT REHEARSAL of the phone call. Write only the words you would say "
                     "aloud, one short turn at a time. To press phone keys write [press 1]. To hang up write "
                     "[end call] after your goodbye.")

    def rehearse_text(self, iid: str, llm: Callable[[str, list], str], *, persona: Optional[str] = None,
                      max_turns: int = 14) -> dict:
        """Play the call out in text: the voice agent's exact instructions against
        a simulated front desk. ``llm(system, messages) -> str`` is any chat model.
        It shows how the BRIEF plays, not the voice, the latency or the phone tree.
        Note: the brief's facts go to whatever model ``llm`` calls."""
        it = self.get(iid)
        if not it:
            raise RuntimeError(f"no call request {iid}")
        b = B.Brief.from_dict(it["brief"])
        caller_sys = B.system_prompt(b, self.cfg.owner_name) + self.TEXT_SIM_NOTE
        desk_sys = B.sim_prompt(b, persona) + "\n\nThis is a text rehearsal: write only what you would say aloud."
        turns: list[tuple[str, str]] = [("desk", "Thanks for calling, front desk, how can I help?")]
        for _ in range(max_turns):
            said = (llm(caller_sys, [{"role": "user" if w == "desk" else "assistant", "content": t}
                                     for w, t in turns]) or "").strip()
            turns.append(("agent", said))
            if "[end call]" in said.lower():
                break
            reply = (llm(desk_sys, [{"role": "user", "content": "[The phone rings and you pick up.]"}]
                         + [{"role": "user" if w == "agent" else "assistant", "content": t} for w, t in turns])
                     or "").strip()
            turns.append(("desk", reply))
            if "[end call]" in reply.lower():
                break
        transcript = "\n".join(f"{'AI' if w == 'agent' else 'Desk'}: {t}" for w, t in turns)
        verdict = llm("You grade phone-call transcripts. Reply with ONE JSON object only.",
                      [{"role": "user", "content": "Fill this JSON schema from the transcript. Use empty strings "
                                                   "when unknown.\nSCHEMA: " + json.dumps(B.OUTCOME_SCHEMA)
                        + "\n\nTRANSCRIPT:\n" + transcript}]) or ""
        m = re.search(r"\{.*\}", verdict, re.S)
        try:
            data = json.loads(m.group(0)) if m else {}
        except ValueError:
            data = {}
        r = {"transcript": transcript, "data": data or {"outcome": "other"}, "summary": "",
             "ended_reason": "text-rehearsal", "seconds": None, "cost": 0, "recording_url": None}
        path = self.write_log(iid, b, r, kind="rehearsal")
        with self.store.locked():
            d = self.store.load_desk()
            d["items"][iid].setdefault("rehearsals", []).append(
                {"at": self.now().isoformat(timespec="seconds"), "persona": persona, "log": str(path),
                 "outcome": (r["data"] or {}).get("outcome")})
            self.store.save_desk(d)
        return {**r, "text": self.result_text(b, r, rehearsal=True), "log": str(path)}
