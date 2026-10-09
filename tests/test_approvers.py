"""The two approvers: a real terminal, and a 👍 proven against LoopMessage's own records."""
import io
from datetime import datetime, timezone

import pytest
from conftest import NOON, brief, make_cfg

from phone_desk.approvers.loopmessage import LoopMessageApprover
from phone_desk.approvers.terminal import TerminalApprover
from phone_desk.desk import Desk
from phone_desk.vapi import Vapi

OWNER_PHONE = "+13125550100"
AGENT_PHONE = "+13125550111"
SECRET = "webhook-secret-for-tests"


# ── terminal ─────────────────────────────────────────────────────────────────
def _terminal_desk(tmp_path, fake_vapi, answer, tty=True):
    cfg = make_cfg(tmp_path)
    ap = TerminalApprover(out=io.StringIO(), input_fn=lambda prompt: answer, isatty=lambda: tty)
    return Desk(cfg, approver=ap, vapi=Vapi(cfg, opener=fake_vapi))


def test_the_terminal_approver_refuses_anything_but_a_real_terminal(tmp_path, fake_vapi):
    desk = _terminal_desk(tmp_path, fake_vapi, "👍", tty=False)
    item = desk.propose(brief(), now=NOON)
    assert "not a TTY" in desk.approve_terminal(item["id"], now=NOON)
    assert desk.get(item["id"])["status"] == "surfaced"


@pytest.mark.parametrize("answer,status", [("👍", "queued"), ("yes {iid}", "queued"), ("y", "surfaced"),
                                           ("yes", "surfaced"), ("", "surfaced"), ("no", "skipped"), ("👎", "skipped")])
def test_the_terminal_approver_needs_an_explicit_answer(tmp_path, fake_vapi, answer, status):
    desk = _terminal_desk(tmp_path, fake_vapi, None)
    item = desk.propose(brief(), now=NOON)
    desk.approver.input_fn = lambda prompt: answer.format(iid=item["id"])
    desk.approve_terminal(item["id"], now=NOON)
    assert desk.get(item["id"])["status"] == status


def test_a_terminal_approval_dials_and_is_bound_to_the_text(tmp_path, fake_vapi):
    desk = _terminal_desk(tmp_path, fake_vapi, "👍")
    item = desk.propose(brief(), now=NOON)
    desk.approve_terminal(item["id"], now=NOON)
    desk.tick(now=NOON)
    assert len(fake_vapi.posts()) == 1


# ── LoopMessage ──────────────────────────────────────────────────────────────
class FakeLoop:
    """LoopMessage's API as the approver sees it: send, message status, webhook history."""

    def __init__(self):
        self.sent = []
        self.status = {}
        self.history = []

    def __call__(self, method, url, headers, params, body):
        assert headers.get("Authorization") == "lm-key"
        if method == "POST" and url.endswith("/message/send/"):
            mid = f"lm-msg-{len(self.sent) + 1}"
            self.sent.append(body)
            self.status[mid] = {"contact": body["contact"], "text": body["text"], "channel": "imessage"}
            return 200, {"success": True, "message_id": mid}
        if "/message/status/" in url:
            mid = url.rstrip("/").rsplit("/", 1)[1]
            return (200, self.status[mid]) if mid in self.status else (404, None)
        if "/webhooks/history/" in url:
            return 200, {"items": list(reversed(self.history)), "num_pages": 1}
        raise AssertionError(url)

    def react(self, mid, reaction="like", contact=OWNER_PHONE, webhook_id=None, when=None):
        wid = webhook_id or f"wh-0000-{len(self.history) + 1:04d}"
        payload = {"event": "message_reaction", "contact": contact, "message_id": mid, "reaction": reaction,
                   "channel": "imessage", "webhook_id": wid}
        self.history.append({"webhook_id": wid, "event_type": "message_reaction",
                             "create_date": (when or datetime(2026, 10, 6, 17, 0, tzinfo=timezone.utc)).isoformat(),
                             "json": payload})
        return payload


@pytest.fixture
def loop():
    return FakeLoop()


@pytest.fixture
def lm_desk(tmp_path, loop, fake_vapi):
    cfg = make_cfg(tmp_path, approver="loopmessage", loopmessage_api_key="lm-key",
                   loopmessage_owner_contact=OWNER_PHONE, loopmessage_webhook_secret=SECRET,
                   external_agent_contact=AGENT_PHONE)
    clock = lambda: datetime(2026, 10, 6, 17, 5, tzinfo=timezone.utc).timestamp()  # noqa: E731
    ap = LoopMessageApprover(cfg, http=loop, clock=clock)
    return Desk(cfg, approver=ap, vapi=Vapi(cfg, opener=fake_vapi))


AUTH = {"Authorization": SECRET}


def test_the_bubble_goes_to_the_owner_only(lm_desk, loop):
    item = lm_desk.propose(brief(), now=NOON)
    assert loop.sent[0]["contact"] == OWNER_PHONE and loop.sent[0]["text"] == item["bubble"]["text"]


def test_a_webhook_without_the_secret_does_nothing(lm_desk, loop):
    item = lm_desk.propose(brief(), now=NOON)
    payload = loop.react(item["bubble"]["mids"][0])
    assert lm_desk.handle_webhook(payload, {"Authorization": "wrong"}, now=NOON)["reason"] == "unauthorized webhook"
    assert lm_desk.handle_webhook(payload, {}, now=NOON)["ok"] is False
    lm_desk.cfg.loopmessage_webhook_secret = ""
    assert lm_desk.handle_webhook(payload, {"Authorization": ""}, now=NOON)["ok"] is False
    assert lm_desk.get(item["id"])["status"] == "surfaced"


def test_a_vendor_proven_thumbs_up_queues_and_the_tick_dials(lm_desk, loop, fake_vapi):
    item = lm_desk.propose(brief(), now=NOON)
    out = lm_desk.handle_webhook(loop.react(item["bubble"]["mids"][0]), AUTH, now=NOON)
    assert "Approved" in out["reply"] and lm_desk.get(item["id"])["status"] == "queued"
    assert "Approved" in loop.sent[-1]["text"]                     # the owner hears back
    lm_desk.tick(now=NOON)
    assert len(fake_vapi.posts()) == 1


def test_a_heart_does_not_place_a_call(lm_desk, loop, fake_vapi):
    item = lm_desk.propose(brief(), now=NOON)
    out = lm_desk.handle_webhook(loop.react(item["bubble"]["mids"][0], reaction="love"), AUTH, now=NOON)
    assert "Only a 👍" in out["reply"] and lm_desk.get(item["id"])["status"] == "surfaced"


def test_a_thumbs_down_drops_it(lm_desk, loop):
    item = lm_desk.propose(brief(), now=NOON)
    lm_desk.handle_webhook(loop.react(item["bubble"]["mids"][0], reaction="dislike"), AUTH, now=NOON)
    assert lm_desk.get(item["id"])["status"] == "skipped"


def test_a_forged_webhook_the_vendor_never_sent_does_nothing(lm_desk, loop):
    item = lm_desk.propose(brief(), now=NOON)
    payload = loop.react(item["bubble"]["mids"][0])
    loop.history.clear()
    out = lm_desk.handle_webhook(payload, AUTH, now=NOON)
    assert "no record" in out["reply"] and lm_desk.get(item["id"])["status"] == "surfaced"


def test_a_thumbs_up_from_someone_else_does_nothing(lm_desk, loop):
    item = lm_desk.propose(brief(), now=NOON)
    out = lm_desk.handle_webhook(loop.react(item["bubble"]["mids"][0], contact="+13125550177"), AUTH, now=NOON)
    assert "does not match" in out["reply"] and lm_desk.get(item["id"])["status"] == "surfaced"


def test_an_old_reaction_does_nothing(lm_desk, loop):
    item = lm_desk.propose(brief(), now=NOON)
    payload = loop.react(item["bubble"]["mids"][0], when=datetime(2026, 10, 6, 13, 0, tzinfo=timezone.utc))
    assert "too old" in lm_desk.handle_webhook(payload, AUTH, now=NOON)["reply"]


def test_a_thumbs_up_on_a_bubble_whose_delivered_text_differs_does_nothing(lm_desk, loop):
    item = lm_desk.propose(brief(), now=NOON)
    mid = item["bubble"]["mids"][0]
    loop.status[mid]["text"] += " (edited)"
    out = lm_desk.handle_webhook(loop.react(mid), AUTH, now=NOON)
    assert "not the request on record" in out["reply"] and lm_desk.get(item["id"])["status"] == "surfaced"


def test_the_same_webhook_cannot_approve_twice(lm_desk, loop):
    a = lm_desk.propose(brief(), now=NOON)
    payload = loop.react(a["bubble"]["mids"][0])
    lm_desk.handle_webhook(payload, AUTH, now=NOON)
    assert "Already" in lm_desk.handle_webhook(payload, AUTH, now=NOON)["reply"]


def test_the_thumbs_up_is_reproven_with_the_vendor_at_dial_time(lm_desk, loop, fake_vapi):
    item = lm_desk.propose(brief(), now=NOON)
    lm_desk.handle_webhook(loop.react(item["bubble"]["mids"][0]), AUTH, now=NOON)
    loop.history.clear()                                   # the vendor no longer has it
    lm_desk.tick(now=NOON)
    assert fake_vapi.posts() == [] and lm_desk.get(item["id"])["status"] == "surfaced"


def test_a_removed_reaction_and_unrelated_reactions_are_ignored(lm_desk, loop):
    item = lm_desk.propose(brief(), now=NOON)
    removed = {**loop.react(item["bubble"]["mids"][0]), "reaction": "", "reaction_type": "like",
               "reaction_event": "removed"}
    assert lm_desk.handle_webhook(removed, AUTH, now=NOON)["ignored"] == "reaction removed"
    assert "not on a call request" in lm_desk.handle_webhook(loop.react("lm-msg-999"), AUTH, now=NOON)["ignored"]


def test_an_inbound_call_request_from_the_external_agent_becomes_a_draft(lm_desk, loop):
    text = ('CALL REQUEST\n{"who": "Lakeview Dental", "to": "312-555-0163", "goal": "Reschedule a cleaning.",'
            ' "facts": ["Patient: Alex Rivera"]}')
    out = lm_desk.handle_webhook({"event": "message_inbound", "contact": AGENT_PHONE, "text": text}, AUTH, now=NOON)
    iid = out["external"]["draft"]
    assert lm_desk.get(iid)["status"] == "draft" and loop.sent == []


def test_a_call_request_from_anyone_else_is_ignored(lm_desk):
    text = 'CALL REQUEST\n{"who": "Lakeview Dental", "to": "312-555-0163", "goal": "Reschedule."}'
    out = lm_desk.handle_webhook({"event": "message_inbound", "contact": "+13125550177", "text": text}, AUTH, now=NOON)
    assert "ignored" in out and lm_desk.store.load_desk()["items"] == {}
