"""The gate: a token is one dial of one exact bubble, inside the window, under the cap."""
from datetime import timedelta
from zoneinfo import ZoneInfo

import pytest
from conftest import GYM, NIGHT, NOON, make_cfg

from phone_desk.brief import content_hash
from phone_desk.gate import TEST_LINES, Gate, GateDenied
from phone_desk.store import Store

TEXT = "📞 Call to approve · Northside Athletic Club\nNumber: (312) 555-0147\n...  [abcd1234]"


@pytest.fixture
def gate(cfg):
    return Gate(cfg, Store(cfg.state_dir))


def _mint(gate, text=TEXT, iid="abcd1234", now=NOON):
    return gate.mint(iid=iid, text_sha=content_hash(text), now=now)


def test_a_token_admits_exactly_one_dial(gate):
    tok = _mint(gate)
    gate.admit(iid="abcd1234", text=TEXT, token=tok, to=GYM, now=NOON)
    with pytest.raises(GateDenied, match="already used"):
        gate.admit(iid="abcd1234", text=TEXT, token=tok, to=GYM, now=NOON)


@pytest.mark.parametrize("edit", [TEXT + " ", TEXT.replace("0147", "0148"), TEXT.replace("Northside", "northside"),
                                  TEXT[:-1], "x" + TEXT])
def test_the_token_is_bound_to_the_exact_text_and_any_edit_invalidates_it(gate, edit):
    tok = _mint(gate)
    with pytest.raises(GateDenied, match="changed after it was approved"):
        gate.admit(iid="abcd1234", text=edit, token=tok, to=GYM, now=NOON)
    # a mismatch does not burn the approval: the text the owner saw still dials once
    gate.admit(iid="abcd1234", text=TEXT, token=tok, to=GYM, now=NOON)


def test_a_token_for_one_request_cannot_dial_another(gate):
    tok = _mint(gate, iid="aaaa1111")
    with pytest.raises(GateDenied, match="different request"):
        gate.admit(iid="bbbb2222", text=TEXT, token=tok, to=GYM, now=NOON)


def test_no_token_or_an_unknown_token_dials_nothing(gate):
    with pytest.raises(GateDenied, match="no approval token"):
        gate.admit(iid="abcd1234", text=TEXT, token=None, to=GYM, now=NOON)
    with pytest.raises(GateDenied, match="not recognized"):
        gate.admit(iid="abcd1234", text=TEXT, token="f" * 32, to=GYM, now=NOON)


def test_an_approval_expires_after_16_hours(gate, cfg):
    assert cfg.approval_ttl_s == 16 * 3600
    minted = NOON.replace(hour=9)
    tok = _mint(gate, now=minted)
    late = minted + timedelta(hours=16, seconds=1)          # 01:00 next day... outside window anyway
    with pytest.raises(GateDenied):
        gate.admit(iid="abcd1234", text=TEXT, token=tok, to=GYM, now=late)
    assert gate.token_expired(tok, late)
    # expiry is checked on its own too, inside the window
    tok2 = _mint(gate, now=NOON - timedelta(hours=17))
    with pytest.raises(GateDenied, match="expired"):
        gate.admit(iid="abcd1234", text=TEXT, token=tok2, to=GYM, now=NOON)


@pytest.mark.parametrize("hour,ok", [(7, False), (8, True), (12, True), (19, True), (20, False), (23, False)])
def test_the_calling_window_is_8_to_20_local(gate, hour, ok):
    tok = _mint(gate)
    when = NOON.replace(hour=hour, minute=0 if hour != 19 else 59)
    if ok:
        gate.admit(iid="abcd1234", text=TEXT, token=tok, to=GYM, now=when)
    else:
        with pytest.raises(GateDenied, match="outside calling hours") as exc:
            gate.admit(iid="abcd1234", text=TEXT, token=tok, to=GYM, now=when)
        assert exc.value.retryable


def test_the_window_uses_the_owner_timezone(gate):
    tok = _mint(gate)
    utc_noon_local_7am = NOON.replace(hour=7).astimezone(ZoneInfo("UTC"))
    with pytest.raises(GateDenied, match="outside calling hours"):
        gate.admit(iid="abcd1234", text=TEXT, token=tok, to=GYM, now=utc_noon_local_7am)


def test_the_daily_cap_counts_every_dial_and_resets_the_next_day(tmp_path):
    cfg = make_cfg(tmp_path, daily_cap=2)
    gate = Gate(cfg, Store(cfg.state_dir))
    for i in range(2):
        gate.admit(iid=f"i{i}", text=TEXT, token=_mint(gate, iid=f"i{i}"), to=GYM, now=NOON)
    tok = _mint(gate, iid="i2")
    with pytest.raises(GateDenied, match="cap of 2") as exc:
        gate.admit(iid="i2", text=TEXT, token=tok, to=GYM, now=NOON)
    assert exc.value.retryable
    tomorrow = NOON + timedelta(hours=-3, days=1)                # 09:00 tomorrow
    gate.admit(iid="i2", text=TEXT, token=_mint(gate, iid="i2", now=tomorrow), to=GYM, now=tomorrow)


def test_the_kill_switch_stops_everything(gate, cfg):
    tok = _mint(gate)
    cfg.kill_file.write_text("off")
    with pytest.raises(GateDenied, match="switched off") as exc:
        gate.admit(iid="abcd1234", text=TEXT, token=tok, to=GYM, now=NOON)
    assert exc.value.retryable
    with pytest.raises(GateDenied, match="switched off"):
        gate.admit_test_line(to=next(iter(TEST_LINES)), now=NOON)


def test_only_the_public_time_line_skips_approval(gate):
    assert list(TEST_LINES) == ["+13034997111"]
    gate.admit_test_line(to="+13034997111", now=NIGHT)            # automated line: no window
    with pytest.raises(GateDenied, match="not a public test line"):
        gate.admit_test_line(to=GYM, now=NOON)


def test_the_test_line_counts_against_the_cap(tmp_path):
    cfg = make_cfg(tmp_path, daily_cap=1)
    gate = Gate(cfg, Store(cfg.state_dir))
    gate.admit_test_line(to="+13034997111", now=NOON)
    with pytest.raises(GateDenied, match="cap"):
        gate.admit_test_line(to="+13034997111", now=NOON)


def test_every_decision_is_in_the_ledger(gate):
    tok = _mint(gate)
    with pytest.raises(GateDenied):
        gate.admit(iid="abcd1234", text=TEXT + "!", token=tok, to=GYM, now=NOON)
    gate.admit(iid="abcd1234", text=TEXT, token=tok, to=GYM, now=NOON)
    decisions = [r["decision"] for r in gate.store.lines("ledger.jsonl")]
    assert decisions == ["MINT", "DENY", "ADMIT"]
