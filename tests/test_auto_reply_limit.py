"""The default packet budget follows the final, UTF-8 encoded node name."""

import pytest

from bot.config import Config, ConfigError, config_from_mapping, load_config


@pytest.mark.parametrize("name", ["Mesh Potato", "Potato", "Madison Mesh1", "\U0001f954 Potato"])
def test_default_limit_tracks_name(name):
    cfg = config_from_mapping({"port": "/dev/fake", "bot_name": name}, env={})
    assert cfg.reply_max_chars == 160 - len(name.encode("utf-8")) - 2
    assert Config(bot_name=name).reply_max_chars == cfg.reply_max_chars
    assert len((name + ": " + "x" * cfg.reply_max_chars).encode("utf-8")) == 160


def test_name_environment_override_precedes_calculation():
    cfg = config_from_mapping({"port": "/dev/fake", "bot_name": "Old name"}, env={
        "MESHAI_BOT_NAME": "Legacy name", "MESHPOTATO_BOT_NAME": "\U0001f954 Potato",
    })
    assert cfg.reply_max_chars == 147


@pytest.mark.parametrize("env", [{}, {"MESHPOTATO_REPLY_MAX_CHARS": "135"},
                                 {"MESHAI_REPLY_MAX_CHARS": "135"}])
def test_optional_smaller_limit(env):
    cfg = config_from_mapping({"port": "/dev/fake", "reply_max_chars": 135}, env=env)
    assert cfg.reply_max_chars == 135


def test_automatic_limit_rejects_name_leaving_no_room():
    with pytest.raises(ConfigError, match="reply_max_chars must be positive"):
        config_from_mapping({"port": "/dev/fake", "bot_name": "x" * 158}, env={})


def test_example_configuration_uses_automatic_limit():
    cfg = load_config("config.example.toml", env={"MESHPOTATO_BOT_NAME": "Potato"})
    assert cfg.reply_max_chars == 152
