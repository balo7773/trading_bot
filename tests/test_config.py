#!/usr/bin/env python3
"""
tests/test_config.py

Verifies configuration loading, credential validation, and fail-safe Demo defaults.
"""
import pytest
from config import load_config


def test_load_config_valid_demo(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "CAPITAL_API_KEY=test_key\n"
        "CAPITAL_IDENTIFIER=test_user\n"
        "CAPITAL_PASSWORD=test_pass\n"
        "TRADING_ENVIRONMENT=DEMO\n"
    )

    cfg = load_config(env_file)
    assert cfg.api_key == "test_key"
    assert cfg.identifier == "test_user"
    assert cfg.password == "test_pass"
    assert cfg.is_demo is True
    assert cfg.environment == "DEMO"


def test_load_config_defaults_to_demo_on_unknown_env(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "CAPITAL_API_KEY=test_key\n"
        "CAPITAL_IDENTIFIER=test_user\n"
        "CAPITAL_PASSWORD=test_pass\n"
        "TRADING_ENVIRONMENT=SOME_TYPO\n"
    )

    cfg = load_config(env_file)
    assert cfg.is_demo is True
    assert cfg.environment == "DEMO"


def test_load_config_explicit_live(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "CAPITAL_API_KEY=test_key\n"
        "CAPITAL_IDENTIFIER=test_user\n"
        "CAPITAL_PASSWORD=test_pass\n"
        "TRADING_ENVIRONMENT=LIVE\n"
    )

    cfg = load_config(env_file)
    assert cfg.is_demo is False
    assert cfg.environment == "LIVE"


def test_load_config_missing_keys_raises_error(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("CAPITAL_API_KEY=test_key\n")

    with pytest.raises(ValueError, match="Missing required configuration: CAPITAL_IDENTIFIER"):
        load_config(env_file)