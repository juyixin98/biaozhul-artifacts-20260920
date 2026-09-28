"""Configuration layer tests."""

from __future__ import annotations

import pytest

from reteapp.config import ConfigError, load_settings


def test_defaults_are_explicit() -> None:
    settings = load_settings(env={})
    assert settings.db_path == ":memory:"
    assert settings.max_fire_rounds == 100
    assert settings.agenda_mode == "manual"


def test_environment_overrides_and_explicit_kwargs_win() -> None:
    settings = load_settings(
        env={"RETE_MAX_FIRE_ROUNDS": "9", "RETE_AGENDA_MODE": "auto"},
        max_fire_rounds=42,
    )
    assert settings.max_fire_rounds == 42
    assert settings.agenda_mode == "auto"


@pytest.mark.parametrize(
    "env,match",
    [
        ({"RETE_MAX_FIRE_ROUNDS": "abc"}, "must be an integer"),
        ({"RETE_MAX_FIRE_ROUNDS": "0"}, "must be positive"),
        ({"RETE_AGENDA_MODE": "turbo"}, "must be one of"),
    ],
)
def test_invalid_configuration_raises_config_error(env, match) -> None:
    with pytest.raises(ConfigError, match=match):
        load_settings(env=env)
