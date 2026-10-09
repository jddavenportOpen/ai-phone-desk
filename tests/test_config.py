"""Config: .env parsing and required settings."""
from pathlib import Path

import pytest

from phone_desk.config import ConfigError, load

ROOT = Path(__file__).resolve().parent.parent


def test_the_shipped_env_example_parses_to_clean_values(tmp_path):
    cfg = load(env_file=ROOT / ".env.example", environ={"PHONE_DESK_STATE_DIR": str(tmp_path)})
    assert cfg.owner_name == "Alex Rivera" and cfg.owner_tz == "America/Chicago"
    assert cfg.vapi_phone_number_id == "" and cfg.loopmessage_owner_contact == ""
    assert cfg.approver == "terminal" and cfg.external_agent_name == "Instinct"


def test_environment_variables_override_the_file(tmp_path):
    env = tmp_path / ".env"
    env.write_text('OWNER_NAME="Alex Rivera"\nOWNER_TZ=America/Chicago\nPHONE_DESK_DAILY_CAP=10 # cap\n')
    cfg = load(env_file=env, environ={"PHONE_DESK_DAILY_CAP": "4", "PHONE_DESK_STATE_DIR": str(tmp_path)})
    assert cfg.daily_cap == 4


@pytest.mark.parametrize("missing", ["OWNER_NAME", "OWNER_TZ"])
def test_owner_name_and_timezone_are_required(tmp_path, missing):
    environ = {"OWNER_NAME": "Alex Rivera", "OWNER_TZ": "America/Chicago", "PHONE_DESK_STATE_DIR": str(tmp_path)}
    environ.pop(missing)
    with pytest.raises(ConfigError, match=missing):
        load(env_file=tmp_path / "none", environ=environ)


def test_a_bad_timezone_or_approver_is_refused(tmp_path):
    base = {"OWNER_NAME": "Alex Rivera", "PHONE_DESK_STATE_DIR": str(tmp_path)}
    with pytest.raises(ConfigError):
        load(env_file=tmp_path / "none", environ={**base, "OWNER_TZ": "Mars/Olympus"})
    with pytest.raises(ConfigError):
        load(env_file=tmp_path / "none", environ={**base, "OWNER_TZ": "UTC", "PHONE_DESK_APPROVER": "email"})
