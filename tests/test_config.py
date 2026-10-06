import os
import re

import pytest

from agent.config import EXAMPLE_DIR, load_env, load_settings


def test_settings_defaults():
    settings = load_settings(EXAMPLE_DIR)
    assert isinstance(settings["live"], bool)
    assert settings["daily_cap"] == 100
    assert settings["fit_threshold"] == 70
    assert settings["daily_budget_usd"] == 100.0
    assert settings["models"] == {"extract": "haiku", "prescore": "haiku", "alert_parse": "haiku", "score": "sonnet", "referral": "sonnet"}
    assert settings["dashboard"] == {"host": "127.0.0.1", "port": 8777}


def test_title_patterns_compile():
    titles = load_settings(EXAMPLE_DIR)["titles"]
    for pattern in titles["allow"] + titles["deny"]:
        re.compile(pattern, re.I)


def test_load_env_sets_missing_and_keeps_existing(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("# comment\nJOBAGENT_NEW='abc'\nJOBAGENT_KEEP=file\n\n")
    monkeypatch.delenv("JOBAGENT_NEW", raising=False)
    monkeypatch.setenv("JOBAGENT_KEEP", "shell")
    load_env(env_file)
    assert os.environ["JOBAGENT_NEW"] == "abc"
    assert os.environ["JOBAGENT_KEEP"] == "shell"
    monkeypatch.delenv("JOBAGENT_NEW")


def test_load_env_ignores_missing_file(tmp_path):
    load_env(tmp_path / "absent.env")


def test_missing_config_points_to_the_example(tmp_path):
    with pytest.raises(SystemExit, match="config/examples/settings.yaml"):
        load_settings(tmp_path)


def test_examples_ship_every_config_file():
    assert {path.name for path in EXAMPLE_DIR.glob("*.yaml")} == {"settings.yaml", "companies.yaml", "profile.yaml", "master_resume.yaml"}
