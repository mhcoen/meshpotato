"""Help-page headroom and verification of the actual radio identity."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from bot import cli
from bot.config import ConfigError, config_from_mapping
from bot.service import ChannelError


@pytest.mark.parametrize("name,cap,prefix", [
    ("Mesh Potato", 147, "/"),
    ("Madison Mesh1", 145, "/"),
    ("Mesh Potato", 147, "!!"),
    ("Madison Mesh1", 145, "!!"),
    ("\U0001f31fMesh Potato", 143, "/"),
])
def test_help_fits_nondefault_names_and_command_prefixes(name, cap, prefix):
    cfg = config_from_mapping({"port": "/dev/fake", "bot_name": name,
                               "reply_max_chars": cap, "command_prefix": prefix}, env={})
    for page in cfg.help_pages:
        assert len(page) <= cap
        assert len(f"{name}: {page}".encode()) <= 160
    assert f"{prefix}roll" in cfg.help_topics["fun"]
    assert f"{prefix}magic8" in cfg.help_topics["fun"]
    assert f"{prefix}serious" in cfg.help_topics["voices"]


def test_help_size_error_identifies_topic_and_relevant_settings():
    with pytest.raises(ConfigError, match="help topic index.*shorten trigger_prefix or command_prefix"):
        config_from_mapping({"port": "/dev/fake", "command_prefix": "!" * 40}, env={})
    with pytest.raises(ConfigError, match="help topic voices.*persona names") as exc:
        config_from_mapping({"port": "/dev/fake", "personas": {
            "nice": "Kind voice.", **{f"personality{i}": "Custom voice." for i in range(12)},
        }}, env={})
    assert "help topic index" not in str(exc.value)


@pytest.mark.parametrize("configured,reported,cap", [
    ("MeshAI", "Mesh Potato", 150),  # unsafe actual packet overhead
    ("Mesh Potato", "MeshAI", 147),  # wrong own-name loop guard
    ("Mesh Potato", "Mesh Potato ", 147),
    ("Mesh Potato", "mesh potato", 147),
    ("Mesh Potato", "M\u00e9sh Potato", 147),
])
async def test_node_name_mismatch_refuses_start_before_any_work(harness, configured, reported, cap):
    h = harness(bot_name=configured, reply_max_chars=cap)
    h.mc.self_info["name"] = reported
    h.mc.commands.get_channel = AsyncMock()
    monitor = SimpleNamespace(start=Mock(), stop=AsyncMock())
    fortune = SimpleNamespace(start=Mock(), stop=AsyncMock())
    h.service.monitor, h.service.fortune = monitor, fortune
    try:
        with pytest.raises(ChannelError, match="does not match bot_name"):
            await h.service.start()
        h.mc.commands.get_channel.assert_not_called()
        monitor.start.assert_not_called()
        fortune.start.assert_not_called()
        assert not h.mc.subscriptions and h.mc.auto_fetch is None
        assert h.service._startup_announcement_task is None
        assert h.service._memory_task is None
        assert not h.sent and not h.backend.calls
    finally:
        await h.service.stop()


@pytest.mark.parametrize("info", [None, {}, {"name": ""}, {"name": None}, {"name": 123}, []])
async def test_unavailable_radio_name_fails_closed(harness, info):
    h = harness()
    h.mc.self_info = info
    try:
        with pytest.raises(ChannelError, match="did not report its node name"):
            await h.service.start()
        assert not h.sent and not h.mc.subscriptions
    finally:
        await h.service.stop()


async def test_cli_mismatch_reports_error_and_disconnects(harness, monkeypatch, capsys):
    h = harness(bot_name="Mesh Potato", reply_max_chars=147)
    h.mc.self_info["name"] = "MeshAI"
    monkeypatch.setattr(cli, "connect", AsyncMock(return_value=h.mc))
    monkeypatch.setattr(cli, "build_service", lambda *args: h.service)
    monkeypatch.setattr(asyncio.get_running_loop(), "add_signal_handler", lambda *args: None)
    assert await cli._run(h.cfg, True, h.log, h.service.references) == 3
    error = capsys.readouterr().err
    assert "does not match bot_name" in error and "Traceback" not in error
    assert h.mc.disconnected and h.backend.closed
    assert not h.sent and not h.mc.subscriptions
