"""Mesh Potato release defaults, packet budgets and migration compatibility."""

import asyncio
import tomllib
from pathlib import Path

import pytest

from bot import __version__
from bot.backends import make_backend
from bot.config import ConfigError, config_from_mapping, load_config
from bot.personas import BUILTIN_PERSONAS
from bot.service import Decision
from tests.conftest import FakeBackend, Harness


def test_release_defaults_and_example_agree():
    cfg = config_from_mapping({"port": "/dev/fake"}, env={})
    example = load_config(Path(__file__).parents[1] / "config.example.toml", env={})
    for value in (cfg, example):
        assert value.bot_name == "Mesh Potato"
        assert value.reply_max_chars == 147
        assert value.personas["serious"] == BUILTIN_PERSONAS["serious"]
        for page in value.help_pages:
            assert len(page) <= value.reply_max_chars
            assert len(f"{value.bot_name}: {page}".encode()) <= 160
    with pytest.raises(ConfigError, match="at most 147"):
        config_from_mapping({"port": "/dev/fake", "reply_max_chars": 148}, env={})


def test_new_environment_overrides_legacy_environment_and_toml():
    cfg = config_from_mapping({"port": "/dev/file"}, env={
        "MESHAI_PORT": "/dev/old", "MESHPOTATO_PORT": "/dev/new",
        "MESHAI_CHANNEL_IDX": "2", "MESHPOTATO_MODEL": "new-model",
    })
    assert cfg.port == "/dev/new" and cfg.channel_idx == 2 and cfg.model == "new-model"
    assert cfg.bot_name == "Mesh Potato"


@pytest.mark.parametrize("env,expected", [
    ({"MESHAI_OPENAI_API_KEY": "legacy-test"}, "Bearer legacy-test"),
    ({"MESHPOTATO_OPENAI_API_KEY": "new-test"}, "Bearer new-test"),
    ({"MESHAI_OPENAI_API_KEY": "legacy-test", "MESHPOTATO_OPENAI_API_KEY": "new-test"}, "Bearer new-test"),
    ({"MESHAI_OPENAI_API_KEY": "legacy-test", "MESHPOTATO_OPENAI_API_KEY": ""}, None),
])
async def test_api_key_migration_precedence(env, expected):
    cfg = config_from_mapping({"port": "/dev/fake", "backend": "openai"}, env={})
    backend = make_backend(cfg, env=env)
    try:
        assert backend._client.headers.get("Authorization") == expected
    finally:
        await backend.aclose()


def test_package_and_both_cli_entry_points():
    project = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())
    assert project["project"]["name"] == "meshpotato"
    assert project["project"]["version"] == __version__
    assert project["project"]["scripts"]["meshpotato"] == project["project"]["scripts"]["meshai"] == "bot.cli:main"


async def test_new_identity_startup_help_serious_roll_and_loop_guard(clock, tmp_path):
    cfg = config_from_mapping({"port": "/dev/fake", "reply_delay_s": 0,
                               "global_burst": 10, "sender_burst": 10,
                               "state_db": str(tmp_path / "state.sqlite3")}, env={})
    h = Harness(cfg, FakeBackend(), clock)
    h.mc.self_info["name"] = cfg.bot_name
    try:
        await h.service.start()
        await asyncio.wait_for(h.service._startup_announcement_task, 1)
        assert h.sent[0][1].startswith(f"Mesh Potato v{__version__}, LLM: ")
        assert h.sent[0][1].endswith(" Try /help.")
        assert await h.say("Alice: /help") is Decision.ANSWERED_HELP
        assert [text for _, text in h.sent[1:]] == list(cfg.help_pages)
        assert await h.say("Alice: /serious") is Decision.PERSONA_SWITCHED
        assert await h.say("Alice: explain bandwidth") is Decision.ANSWERED
        assert BUILTIN_PERSONAS["serious"] in h.backend.calls[-1][0]["content"]
        assert await h.say("Alice: /roll 3 8") is Decision.ANSWERED_ROLL
        assert await h.say("Mesh Potato: @[Alice] Four.") is Decision.DROP_LOOP_GUARD
        for _, text in h.sent:
            assert len(text) <= 147
            assert len(f"Mesh Potato: {text}".encode()) <= 160
    finally:
        await h.service.stop()


async def test_renamed_packet_budget_shortens_instead_of_truncating(clock):
    cfg = config_from_mapping({"port": "/dev/fake", "reply_delay_s": 0}, env={})
    sender = "\U0001f31f" * 6 + "Andy"
    prefix = f"@[{sender}] "
    available = 147 - len(prefix.encode())
    oversized = "x" * available + "."
    h = Harness(cfg, FakeBackend(replies=[oversized, "Short answer."]), clock)
    try:
        assert await h.say(f"{sender}: hello") is Decision.ANSWERED
        assert len(h.backend.calls) == 2
        assert h.sent == [(1, prefix + "Short answer.")]
        assert len(f"Mesh Potato: {h.sent[0][1]}".encode()) <= 160
    finally:
        await h.service.stop()
