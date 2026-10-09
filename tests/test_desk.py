"""The desk: propose -> surface -> 👍 -> tick dials -> tick reports. Nothing else dials."""
from datetime import timedelta

import pytest
from conftest import GYM, NIGHT, NOON, approve, brief, http_error, make_cfg

from phone_desk.desk import STUCK_DIAL_S, Desk
from phone_desk.vapi import Vapi


def _surfaced(desk, **kw):
    return desk.propose(brief(**kw), now=NOON)


def test_propose_sends_the_owner_the_bubble(desk, approver):
    item = _surfaced(desk)
    assert item["status"] == "surfaced" and item["bubble"]["mids"] == ["mid-1"]
    assert approver.sent == [(item["bubble"]["text"], item["id"])]
    assert "Alex Rivera" in item["bubble"]["text"] and item["id"] in item["bubble"]["text"]


def test_a_held_draft_reaches_nobody_until_surfaced(desk, approver):
    item = desk.propose(brief(), hold=True, now=NOON)
    assert item["status"] == "draft" and approver.sent == []
    assert desk.surface(item["id"], now=NOON)["status"] == "surfaced" and len(approver.sent) == 1


def test_a_dry_run_records_and_sends_nothing(desk, approver):
    item = desk.propose(brief(), dry_run=True, now=NOON)
    assert approver.sent == [] and desk.get(item["id"]) is None


def test_approve_then_tick_dials_the_exact_number_once(desk, fake_vapi):
    item = _surfaced(desk)
    assert "Approved" in approve(desk, item["id"])
    assert desk.get(item["id"])["status"] == "queued"
    assert desk.tick(now=NOON)["dialed"] == 1
    posts = fake_vapi.posts()
    assert len(posts) == 1
    body = posts[0][2]
    assert body["customer"]["number"] == GYM and body["phoneNumberId"] == "num-test"
    assert "Member number: NAC-20417" in body["assistant"]["model"]["messages"][0]["content"]
    assert desk.get(item["id"])["status"] == "in-call"
    desk.tick(now=NOON)                                     # still in call: no second dial
    assert len(fake_vapi.posts()) == 1


def test_an_unapproved_request_never_dials(desk, fake_vapi):
    item = _surfaced(desk)
    desk.tick(now=NOON)
    assert fake_vapi.posts() == [] and desk.get(item["id"])["status"] == "surfaced"
    assert "isn't approved" in desk.dial(item["id"], now=NOON)
    assert fake_vapi.posts() == []


def test_a_hand_written_queued_row_without_a_token_dials_nothing(desk, fake_vapi):
    item = _surfaced(desk)
    desk._update(item["id"], status="queued", approval={"evidence": {}, "token": "f" * 32})
    desk.tick(now=NOON)
    assert fake_vapi.posts() == []
    assert desk.get(item["id"])["status"] in ("failed", "expired")


def test_a_drop_drops_it(desk, fake_vapi):
    item = _surfaced(desk)
    assert "Dropped" in desk._record(item["id"], "skip", {"kind": "fake", "key": "k1"}, NOON)
    desk.tick(now=NOON)
    assert fake_vapi.posts() == [] and desk.get(item["id"])["status"] == "skipped"


def test_a_like_outside_calling_hours_waits_for_the_window(desk, fake_vapi):
    item = _surfaced(desk)
    assert "outside calling hours" in approve(desk, item["id"], now=NIGHT)
    desk.tick(now=NIGHT)
    assert fake_vapi.posts() == []
    desk.tick(now=NOON + timedelta(days=1, hours=-3))          # 09:00 next morning, 11 h later
    assert len(fake_vapi.posts()) == 1


def test_an_approval_that_lapses_before_the_window_expires_instead_of_dialing(tmp_path, approver, fake_vapi):
    cfg = make_cfg(tmp_path, approval_ttl_s=3600)
    desk = Desk(cfg, approver=approver, vapi=Vapi(cfg, opener=fake_vapi))
    item = desk.propose(brief(), now=NIGHT)
    approve(desk, item["id"], now=NIGHT)
    desk.tick(now=NOON + timedelta(days=1))
    assert fake_vapi.posts() == [] and desk.get(item["id"])["status"] == "expired"
    assert "expired" in approver.notes[-1]


def test_the_same_approval_cannot_approve_twice(desk):
    item = _surfaced(desk)
    approve(desk, item["id"], key="k1")
    assert "Already" in approve(desk, item["id"], key="k1")
    other = _surfaced(desk)
    assert "already acted on" in approve(desk, other["id"], key="k1")     # a spent 👍 can't move to another


def test_a_bubble_over_a_day_old_cannot_be_approved(desk):
    item = _surfaced(desk)
    assert "over a day old" in approve(desk, item["id"], now=NOON + timedelta(hours=25))
    assert desk.get(item["id"])["status"] == "expired"


def test_editing_the_brief_after_approval_dials_nothing(desk, fake_vapi):
    item = _surfaced(desk)
    approve(desk, item["id"])
    data = desk.store.load_desk()
    data["items"][item["id"]]["brief"]["goal"] = "Upgrade Alex to the premium annual plan."
    desk.store.save_desk(data)
    assert "changed after you approved" in desk.dial(item["id"], now=NOON)
    assert fake_vapi.posts() == []


def test_editing_the_bubble_text_after_approval_dials_nothing(desk, fake_vapi):
    item = _surfaced(desk)
    approve(desk, item["id"])
    data = desk.store.load_desk()
    data["items"][item["id"]]["bubble"]["text"] += " "
    desk.store.save_desk(data)
    desk.dial(item["id"], now=NOON)
    assert fake_vapi.posts() == []


def test_an_approval_that_cannot_be_reproven_at_dial_time_dials_nothing(desk, approver, fake_vapi):
    item = _surfaced(desk)
    approve(desk, item["id"])
    approver.reprove_error = "the vendor no longer shows the 👍"
    out = desk.dial(item["id"], now=NOON)
    assert "👍 the request again" in out and fake_vapi.posts() == []
    assert desk.get(item["id"])["status"] == "surfaced"


def test_the_kill_switch_holds_an_approved_call(desk, cfg, fake_vapi):
    item = _surfaced(desk)
    approve(desk, item["id"])
    cfg.kill_file.write_text("off")
    desk.tick(now=NOON)
    assert "switched off" in desk.dial(item["id"], now=NOON)
    assert fake_vapi.posts() == [] and desk.get(item["id"])["status"] == "queued"
    cfg.kill_file.unlink()
    desk.tick(now=NOON)
    assert len(fake_vapi.posts()) == 1


def test_a_timeout_after_the_dial_request_is_unknown_and_never_redialed(desk, fake_vapi, approver):
    item = _surfaced(desk)
    approve(desk, item["id"])
    fake_vapi.fail_post = TimeoutError("timed out")
    out = desk.dial(item["id"], now=NOON)
    assert "Not sure" in out and desk.get(item["id"])["status"] == "unknown"
    fake_vapi.fail_post = None
    for _ in range(3):
        desk.tick(now=NOON)
    assert len(fake_vapi.posts()) == 1                      # the one attempt, never again


def test_a_server_error_is_unknown_too(desk, fake_vapi):
    item = _surfaced(desk)
    approve(desk, item["id"])
    fake_vapi.fail_post = http_error(502)
    desk.dial(item["id"], now=NOON)
    assert desk.get(item["id"])["status"] == "unknown"


def test_a_4xx_from_vapi_is_failed_and_says_nothing_was_dialed(desk, fake_vapi):
    item = _surfaced(desk)
    approve(desk, item["id"])
    fake_vapi.fail_post = http_error(400)
    assert "Nothing was dialed" in desk.dial(item["id"], now=NOON)
    assert desk.get(item["id"])["status"] == "failed"


def test_a_stuck_dialing_row_is_reported_not_redialed(desk, approver, fake_vapi):
    item = _surfaced(desk)
    desk._update(item["id"], status="dialing", dialing_ts=(NOON - timedelta(seconds=STUCK_DIAL_S + 5)).timestamp())
    desk.tick(now=NOON)
    assert desk.get(item["id"])["status"] == "unknown" and "can't tell" in approver.notes[-1]
    assert fake_vapi.posts() == []


def test_the_daily_cap_holds_the_call_and_says_so_once(tmp_path, approver, fake_vapi):
    cfg = make_cfg(tmp_path, daily_cap=1)
    desk = Desk(cfg, approver=approver, vapi=Vapi(cfg, opener=fake_vapi))
    a, b = desk.propose(brief(), now=NOON), desk.propose(brief(who="Lakeview Dental"), now=NOON)
    approve(desk, a["id"])
    approve(desk, b["id"])
    for _ in range(3):
        desk.tick(now=NOON)
    assert len(fake_vapi.posts()) == 1
    assert sum("cap" in n for n in approver.notes) == 1
    assert desk.get(b["id"])["status"] == "queued"


ENDED = {"status": "ended", "endedReason": "assistant-ended-call", "startedAt": "2026-10-06T17:00:00Z",
         "endedAt": "2026-10-06T17:04:00Z", "cost": 0.62,
         "artifact": {"transcript": "AI: Hi, this is an AI assistant...", "recordingUrl": "https://example.com/r.wav"},
         "analysis": {"summary": "Left a voicemail for the manager.", "structuredData": {
             "outcome": "partial", "what_they_did": "Front desk logged the cancellation request",
             "confirmation": "CX-55821", "staff_name": "Jordan", "money": "",
             "next_step_for_owner": "Expect a call from the manager about the refund", "follow_up_by": "2026-10-09"}}}


def test_the_tick_writes_the_outcome_and_transcript_and_tells_the_owner(desk, approver, fake_vapi):
    item = _surfaced(desk)
    approve(desk, item["id"])
    desk.tick(now=NOON)
    fake_vapi.calls["call-1"] = {**ENDED, "id": "call-1"}
    assert desk.tick(now=NOON)["finalized"] == 1
    note = approver.notes[-1]
    assert "CX-55821" in note and "Jordan" in note and "manager" in note
    it = desk.get(item["id"])
    assert it["status"] == "done"
    assert "AI: Hi, this is an AI assistant" in open(it["log"]).read()
    assert desk.tick(now=NOON)["finalized"] == 0            # reported once


def test_no_answer_is_reported_plainly():
    r = Desk.artifact({"status": "ended", "endedReason": "customer-did-not-answer"})
    assert "no answer" in Desk.result_text(brief(), r)


def test_the_line_test_dials_nist_without_approval_and_respects_the_kill_switch(desk, cfg, fake_vapi):
    real = fake_vapi.__call__

    def opener(req, timeout=None):
        resp = real(req, timeout)
        for c in fake_vapi.calls.values():
            c.update(status="ended", endedReason="assistant-ended-call", startedAt="2026-10-06T17:00:00Z",
                     endedAt="2026-10-06T17:01:20Z",
                     artifact={"transcript": "At the tone, fourteen hours, Coordinated Universal Time."})
        return resp
    desk.vapi = Vapi(cfg, opener=opener)
    res = desk.linetest(wait_s=30, sleep=lambda s: None)
    assert res["ok"], res
    assert fake_vapi.posts()[0][2]["customer"]["number"] == "+13034997111"
    cfg.kill_file.write_text("off")
    res = desk.linetest(wait_s=30, sleep=lambda s: None)
    assert not res["ok"] and "switched off" in res["error"] and len(fake_vapi.posts()) == 1


def test_a_text_rehearsal_plays_the_brief_against_a_simulated_desk(desk):
    item = _surfaced(desk)
    script = iter(["Hi, this is an AI assistant calling on behalf of Alex Rivera.",
                   "Sure, what's the member number?",
                   "It's NAC-20417. Thank you, goodbye. [end call]"])
    seen_systems = []

    def llm(system, messages):
        seen_systems.append(system)
        if system.startswith("You grade"):
            return '{"outcome": "done", "what_they_did": "cancelled", "next_step_for_owner": ""}'
        return next(script)
    res = desk.rehearse_text(item["id"], llm)
    assert "NAC-20417" in res["transcript"] and res["data"]["outcome"] == "done"
    assert any("HARD RULES" in s for s in seen_systems)
    assert desk.get(item["id"])["rehearsals"][0]["outcome"] == "done"


@pytest.mark.parametrize("status", ["draft", "skipped", "expired", "failed", "unknown", "done"])
def test_no_terminal_or_waiting_status_ever_dials(desk, fake_vapi, status):
    item = _surfaced(desk)
    approve(desk, item["id"])
    desk._update(item["id"], status=status)
    desk.tick(now=NOON)
    desk.dial(item["id"], now=NOON)
    assert fake_vapi.posts() == []


def test_a_missing_vapi_key_fails_before_the_gate_and_burns_no_approval(desk, cfg, fake_vapi):
    item = _surfaced(desk)
    approve(desk, item["id"])
    cfg.vapi_api_key = ""
    assert "Nothing was dialed" in desk.dial(item["id"], now=NOON)
    assert fake_vapi.requests == []
    assert not any(r["decision"] == "ADMIT" for r in desk.store.lines("ledger.jsonl"))
