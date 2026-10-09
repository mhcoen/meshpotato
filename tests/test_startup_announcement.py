"""Startup identification shares the version and the existing safe send path."""

import asyncio
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest
from meshcore import EventType

from bot import __version__
from bot.cli import main
from bot.service import ChannelError
from tests.conftest import FakeBackend, Harness, make_config
from tests.test_queue import until


def test_cli_and_project_use_the_announced_version(capsys):
    project = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())
    assert project["project"]["version"] == __version__
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"meshpotato {__version__}"


@pytest.mark.parametrize("name,display", [("MeshAI", "MeshAI"), ("OtherAI", "OtherAI"), ("M\u00e9shAI", "MeshAI")])
async def test_start_announces_name_and_version_once_without_model(harness, name, display):
    h = harness(bot_name=name)
    await asyncio.gather(h.service.start(), h.service.start())
    await asyncio.wait_for(h.service._startup_announcement_task, 1)
    assert h.sent == [(1, f"{display} v{__version__}: Ask about radio, science, jokes, or a poem. Try /help for examples.")]
    assert len(h.sent[0][1]) <= h.cfg.reply_max_chars
    assert len(f"{h.cfg.bot_name}: {h.sent[0][1]}".encode("utf-8")) <= 160
    assert h.backend.calls == []
    assert h.limiter.snapshot()["global_tokens"] == 0
    assert [r for r in h.records if r["event"] == "startup"][0]["version"] == __version__
    await h.service.start()
    await h.service._on_connected(SimpleNamespace(payload={}))
    assert len(h.sent) == 1
    await h.service.stop()


async def test_start_announcement_defers_when_paused_and_expires(harness, clock):
    h = harness()
    h.service.timer_tick_s = 0.001
    h.limiter.set_global_factor(0)
    await h.service.start()
    task = h.service._startup_announcement_task
    await until(lambda: task in h.service._requests)
    assert not task.done() and h.sent == []
    assert h.limiter.snapshot()["global_tokens"] == 1
    clock.advance(601)
    await asyncio.wait_for(task, 1)
    assert h.sent == []
    assert any(r["event"] == "announce_failed" and r["what"] == "startup" for r in h.records)
    await h.service.stop()


async def test_start_announcement_waits_for_rate_token(harness, clock):
    h = harness()
    assert h.limiter.allow("Someone").allowed
    h.service.timer_tick_s = 0.001
    await h.service.start()
    task = h.service._startup_announcement_task
    await until(lambda: task in h.service._requests)
    assert h.sent == []
    clock.advance(15)
    await asyncio.wait_for(task, 1)
    assert h.sent == [(1, f"MeshAI v{__version__}: Ask about radio, science, jokes, or a poem. Try /help for examples.")]
    await h.service.stop()


async def test_start_announcement_resumes_after_congestion(harness):
    h = harness()
    h.service.timer_tick_s = 0.001
    h.limiter.set_global_factor(0)
    await h.service.start()
    task = h.service._startup_announcement_task
    await until(lambda: task in h.service._requests)
    h.limiter.set_global_factor(1)
    await asyncio.wait_for(task, 1)
    assert len(h.sent) == 1
    await h.service.stop()


async def test_start_announcement_detector_exception_blocks_without_token(harness, monkeypatch):
    h = harness()  # Configuration passed before the detector became unavailable.
    def broken(*args, **kwargs):
        raise RuntimeError("detector unavailable")

    monkeypatch.setattr("bot.guard.detect_prompt_injection", broken)
    await h.service.start()
    await asyncio.wait_for(h.service._startup_announcement_task, 1)
    assert h.sent == [] and h.limiter.snapshot()["global_tokens"] == 1
    assert h.backend.calls == []
    await h.service.stop()


async def test_start_announcement_send_error_is_not_retried(harness, clock):
    h = harness()
    h.mc.commands.send_result_type = EventType.ERROR
    await h.service.start()
    await asyncio.wait_for(h.service._startup_announcement_task, 1)
    clock.advance(60)
    await h.service.start()
    assert len(h.sent) == 1
    assert any(r["event"] == "announce_failed" and r["reason"] == "send-failed" for r in h.records)
    await h.service.stop()


async def test_shutdown_cancels_start_announcement_during_initial_hold(harness, monkeypatch):
    h = harness()
    entered = asyncio.Event()

    async def hold(received_at):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(h.service, "_hold_for_quiet_channel", hold)
    await h.service.start()
    task = h.service._startup_announcement_task
    await entered.wait()
    await h.service.stop()
    assert task.cancelled()
    assert h.sent == [] and h.service._startup_announcement_task is None


async def test_shutdown_cancels_start_announcement_waiting_for_capacity(harness):
    h = harness()
    h.limiter.set_global_factor(0)
    await h.service.start()
    task = h.service._startup_announcement_task
    await until(lambda: task in h.service._requests)
    await h.service.stop()
    assert task.cancelled() and h.sent == [] and not h.service._requests


async def test_failed_start_has_no_announcement(harness):
    h = harness(channel_name="")
    with pytest.raises(ChannelError):
        await h.service.start()
    assert h.service._startup_announcement_task is None
    assert h.sent == []
    await h.service.stop()


@pytest.mark.parametrize("backend,model", [("ollama", "gemma3:12b"), ("openai", "local-model")])
async def test_start_introduces_capabilities_for_either_backend(clock, backend, model):
    h = Harness(make_config(backend=backend, model=model), FakeBackend(), clock)
    await h.service.start()
    await asyncio.wait_for(h.service._startup_announcement_task, 1)
    assert h.sent == [(1, f"MeshAI v{__version__}: Ask about radio, science, jokes, or a poem. Try /help for examples.")]
    assert h.backend.calls == []
    await h.service.stop()


@pytest.mark.parametrize("model", ["model" * 40, "caf\u00e9", "model\nname"])
async def test_unprintable_or_long_model_name_does_not_break_introduction(harness, model):
    h = harness(model=model)
    await h.service.start()
    await asyncio.wait_for(h.service._startup_announcement_task, 1)
    assert len(h.sent) == 1 and h.backend.calls == []
    assert "Try /help for examples." in h.sent[0][1]
    assert model not in h.sent[0][1]
    await h.service.stop()


async def test_startup_help_hint_uses_configured_prefixes(harness):
    h = harness(trigger_prefix="!ai ", command_prefix="!")
    try:
        await h.service.start()
        await asyncio.wait_for(h.service._startup_announcement_task, 1)
        assert len(h.sent) == 1
        assert h.sent[0][1].endswith(" Try !ai !help for examples.")
    finally:
        await h.service.stop()


async def test_startup_advertises_enabled_live_topics(harness):
    h = harness(web_enabled=True)
    try:
        await h.service.start()
        await asyncio.wait_for(h.service._startup_announcement_task, 1)
        assert "weather, sports, traffic" in h.sent[0][1]
        assert len(f"MeshAI: {h.sent[0][1]}".encode()) <= 160
    finally:
        await h.service.stop()


async def test_quiet_start_keeps_normal_replies_and_reconnect_quiet(harness):
    from bot.service import Decision
    h = harness(announce_startup=False)
    try:
        await h.service.start()
        assert h.service._startup_announcement_task is None
        assert h.sent == [] and h.mc.auto_fetch
        assert h.limiter.snapshot()['global_tokens'] == 1
        await h.service._on_connected(SimpleNamespace(payload={}))
        await h.service.start()
        assert h.sent == []
        assert await h.say('Alice: help') is Decision.ANSWERED_HELP
        assert h.sent == [(1, h.cfg.help_message)]
    finally:
        await h.service.stop()


@pytest.mark.parametrize('headless', [False, True])
@pytest.mark.parametrize('configured,flag,expected', [(True,True,False),(True,False,True),(False,False,False)])
def test_no_announce_cli_override_without_process_or_radio_io(monkeypatch, headless, configured, flag, expected):
    from contextlib import nullcontext
    from unittest.mock import Mock
    from bot import cli
    cfg = make_config(announce_startup=configured)
    monkeypatch.setattr(cli, 'load_config', lambda *args: cfg)
    monkeypatch.setattr(cli, 'checked_references', lambda *args: ())
    owner = Mock()
    monkeypatch.setattr(cli, 'SingleInstance', lambda: nullcontext(owner))
    def run(args, actual, references):
        assert actual.announce_startup is expected
        assert args.headless is headless
        return 0
    monkeypatch.setattr(cli, '_main_run', run)
    assert cli.main((['--headless'] if headless else []) + (['--no-announce'] if flag else [])) == 0
    owner.stop_others.assert_called_once()


def test_quiet_start_can_be_configured_by_environment():
    from bot.config import config_from_mapping
    cfg = config_from_mapping({'port':'/dev/fake'}, env={'MESHPOTATO_ANNOUNCE_STARTUP':'false'})
    assert cfg.announce_startup is False
