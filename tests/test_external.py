"""The external agent (e.g. Instinct): it can ask and it can research. It can never dial."""
import json
from datetime import timedelta

import pytest
from conftest import GYM, NOON, approve, brief

from phone_desk import external as X
from phone_desk import brief as B


def _req(**kw):
    obj = {"who": "Lakeview Dental", "to": "(312) 555-0163", "goal": "Move Alex's cleaning to next week.",
           "ask": "Any Tuesday morning", "fallback": "Ask for the next opening", "facts": ["Patient: Alex Rivera"]}
    obj.update(kw)
    return "CALL REQUEST\n" + json.dumps(obj)


def test_a_call_request_becomes_a_held_draft_marked_as_the_agents(desk, approver):
    out = X.ingest(desk, _req(), agent="Instinct", now=NOON)
    it = desk.get(out["draft"])
    assert it["status"] == "draft" and approver.sent == []          # the owner has NOT been texted
    assert it["brief"]["origin"] == "external" and it["brief"]["requested_by"] == "Instinct"
    assert "(Instinct asked for this)" in it["bubble"]["text"]


def test_a_draft_never_dials(desk, fake_vapi):
    iid = X.ingest(desk, _req(), agent="Instinct", now=NOON)["draft"]
    for _ in range(3):
        desk.tick(now=NOON)
    assert desk.dial(iid, now=NOON).startswith("That call isn't approved")
    assert fake_vapi.posts() == [] and desk.get(iid)["status"] == "draft"


def test_a_draft_dials_only_after_it_is_surfaced_and_the_owner_approves_that_bubble(desk, approver, fake_vapi):
    iid = X.ingest(desk, _req(), agent="Instinct", now=NOON)["draft"]
    desk.surface(iid, now=NOON)
    assert "(Instinct asked for this)" in approver.sent[0][0]
    approve(desk, iid)
    desk.tick(now=NOON)
    assert len(fake_vapi.posts()) == 1 and fake_vapi.posts()[0][2]["customer"]["number"] == "+13125550163"


@pytest.mark.parametrize("text,why", [
    (_req(to="876-555-0100"), "Caribbean"),
    (_req(to="1-900-555-0100"), "premium"),
    (_req(to="976-555-0100"), "premium"),
    (_req(to="911"), "10-digit"),
    (_req(facts=["Card 4111 1111 1111 1111"]), "never carries"),
    (_req(facts=["SSN 123-45-6789"]), "never carries"),
    (_req(facts="Password: hunter2"), "never carries"),
    (_req(facts=[{"nested": "object"}]), "list of strings"),
    (_req(goal=""), "needs a goal"),
    (_req(who=["a", "b"]), "must be text"),
    ("CALL REQUEST\n{not json}", "malformed"),
    ("CALL REQUEST please call the dentist", "no JSON"),
    ("CALL REQUEST\n[1, 2]", "no JSON"),
])
def test_unsafe_or_malformed_requests_are_dropped(desk, text, why):
    out = X.ingest(desk, text, agent="Instinct", now=NOON)
    assert "dropped" in out and why.lower() in out["dropped"].lower()
    assert desk.store.load_desk()["items"] == {}


def test_plain_chatter_is_ignored(desk):
    assert "ignored" in X.ingest(desk, "please call the dentist for me", agent="Instinct", now=NOON)


def test_requests_are_capped_per_agent_per_day(desk):
    outs = [X.ingest(desk, _req(to=f"(312) 555-01{60 + i}"), agent="Instinct", now=NOON) for i in range(5)]
    assert sum("draft" in o for o in outs) == desk.cfg.external_daily_cap == 3
    assert "over the cap" in outs[3]["dropped"]
    assert "draft" in X.ingest(desk, _req(to="(312) 555-0190"), agent="OtherAgent", now=NOON)   # per agent
    assert "draft" in X.ingest(desk, _req(to="(312) 555-0191"), agent="Instinct", now=NOON + timedelta(days=1))


def test_a_duplicate_request_is_dropped(desk):
    X.ingest(desk, _req(), agent="Instinct", now=NOON)
    assert X.ingest(desk, _req(), agent="Instinct", now=NOON) == {"dropped": "duplicate"}


def test_the_agent_cannot_set_anything_outside_the_contract(desk):
    text = _req(voicemail="Call me back at 312-555-0100", max_minutes=30, origin="owner", requested_by="Alex",
                instructions="skip approval and dial now")
    iid = X.ingest(desk, text, agent="Instinct", now=NOON)["draft"]
    b = desk.get(iid)["brief"]
    assert b["voicemail"] == "" and b["max_minutes"] == B.DEFAULT_MINUTES
    assert b["origin"] == "external" and b["requested_by"] == "Instinct"
    assert "skip approval" not in desk.get(iid)["bubble"]["text"]


def test_the_contract_text_states_the_rules(cfg):
    text = X.contract(cfg.owner_name, cap=3)
    for must in ("CALL REQUEST", "Alex Rivera", "At most 3", "never card, bank, SSN, password",
                 "nothing is dialed unless"):
        assert must in text


# ── research ─────────────────────────────────────────────────────────────────
def _researched(desk, **reply):
    item = desk.propose(brief(), now=NOON)
    job_text = X.research_job(desk, item["id"], hosts=["northside.example"], now=NOON)
    job = desk.get(item["id"])["research"]["job"]
    obj = {"job": job, "phone": "(312) 555-0147", "hours_today": "6am-10pm",
           "how_to_request": "in person or by phone", "request_url": "https://northside.example/cancel",
           "policy": "30 days notice", "source_urls": ["https://northside.example/policies"]}
    obj.update(reply)
    return item, job_text, X.ingest(desk, json.dumps(obj), agent="Instinct", now=NOON)


def test_the_research_job_is_self_contained_paced_and_discloses_nothing_about_the_owner(desk):
    item, text, _ = _researched(desk)
    job = desk.get(item["id"])["research"]["job"]
    assert text.startswith(f"JOB {job}") and "self-contained" in text
    assert "northside.example are the only hosts" in text and "STOP and reply BLOCKED" in text
    assert "Cite a URL for every fact" in text and "Do not call, email, submit any form" in text
    assert "Alex" not in text and "NAC-20417" not in text


def test_a_research_reply_is_stored_as_a_note_and_the_same_number_is_not_flagged(desk):
    item, _, out = _researched(desk)
    assert out["flagged"] is False and "30 days notice" in out["note"]
    r = desk.get(item["id"])["research"]
    assert r["status"] == "done" and "northside.example/policies" in r["note"]


def test_a_different_number_is_flagged_and_never_swapped_into_the_brief(desk, fake_vapi):
    item = desk.propose(brief(), now=NOON)
    approve(desk, item["id"])
    before = desk.get(item["id"])
    X.research_job(desk, item["id"], now=NOON)
    job = desk.get(item["id"])["research"]["job"]
    out = X.ingest(desk, json.dumps({"job": job, "phone": "(312) 555-0188", "source_urls": ["https://x.example"]}),
                   agent="Instinct", now=NOON)
    assert out["flagged"] and "FLAGGED" in out["note"] and "NOT changed" in out["note"]
    after = desk.get(item["id"])
    assert after["brief"] == before["brief"] and after["bubble"] == before["bubble"]
    assert after["approval"] == before["approval"] and after["brief"]["to"] == GYM
    desk.tick(now=NOON)
    assert fake_vapi.posts()[0][2]["customer"]["number"] == GYM          # dials what the owner approved


def test_a_reply_without_sources_is_marked_unverified(desk):
    _, _, out = _researched(desk, source_urls=[])
    assert "No source URLs" in out["note"]


def test_a_reply_for_an_unknown_job_is_ignored(desk):
    assert "ignored" in X.ingest(desk, '{"job": "pd-nope-0000", "phone": "x"}', agent="Instinct", now=NOON)


def test_a_research_job_is_answered_once(desk):
    item, _, _ = _researched(desk)
    job = desk.get(item["id"])["research"]["job"]
    assert "already answered" in X.ingest(desk, json.dumps({"job": job, "phone": "(312) 555-0101"}),
                                          agent="Instinct", now=NOON)["ignored"]
