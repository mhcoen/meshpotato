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


@pytest.mark.parametrize('failure', [False, True])
async def test_persistent_welcome_not_replayed_after_restart(harness, tmp_path, failure):
    path = str(tmp_path/'welcome.sqlite3')
    h = harness(state_db=path)
    if failure:
        h.mc.commands.raise_on_send = OSError('ambiguous radio failure')
    await h.service.start()
    await asyncio.wait_for(h.service._startup_announcement_task, 1)
    assert h.service._state_store.welcome_attempted()
    await h.service.stop()
    restarted = harness(state_db=path)
    await restarted.service.start()
    await asyncio.wait_for(restarted.service._startup_announcement_task, 1)
    assert restarted.sent == []
    await restarted.service.stop()


async def test_suppressed_or_cancelled_welcome_remains_due(harness, tmp_path):
    path = str(tmp_path/'welcome.sqlite3')
    quiet = harness(state_db=path, announce_startup=False)
    await quiet.service.start()
    assert not quiet.service._state_store.welcome_attempted()
    await quiet.service.stop()
    paused = harness(state_db=path)
    paused.limiter.set_global_factor(0)
    await paused.service.start()
    await until(lambda: paused.service._startup_announcement_task in paused.service._requests)
    assert not paused.service._state_store.welcome_attempted()
    await paused.service.stop()
    active = harness(state_db=path)
    await active.service.start()
    await asyncio.wait_for(active.service._startup_announcement_task, 1)
    assert len(active.sent) == 1
    await active.service.stop()


async def test_welcome_save_failure_prevents_radio_attempt(harness, tmp_path, monkeypatch):
    from bot.storage import StateError
    h = harness(state_db=str(tmp_path/'welcome.sqlite3'))
    await h.service.prepare()
    def fail():
        raise StateError('disk full')
    monkeypatch.setattr(h.service._state_store, 'claim_welcome', fail)
    await h.service.start()
    await asyncio.wait_for(h.service._startup_announcement_task, 1)
    assert not h.sent and not h.service._state_store.welcome_attempted()
    await h.service.stop()


async def test_three_channel_first_welcomes_and_independent_versions(tmp_path, monkeypatch):
    import io
    from bot.cli import build_service
    from bot.jsonlog import EventLog
    from bot.traffic.channel import TrafficChannel
    from tests.test_channels import ChannelRadio
    from tests.test_chess import FakeEngine
    from tests.test_traffic_channel import FakeAPI
    monkeypatch.setattr('bot.cli.make_backend', lambda cfg: FakeBackend())
    monkeypatch.setattr('bot.chess_engine.Stockfish', lambda *args: FakeEngine())
    monkeypatch.setattr('bot.traffic.api.Wisconsin511', FakeAPI)
    monkeypatch.setattr(TrafficChannel, 'start', lambda self: None)
    monkeypatch.setattr('bot.traffic.TrafficCache.start', lambda self: None)
    cfg = make_config(chess_channel_idx=3, traffic_channel_idx=2,
                      state_db=str(tmp_path/'all.sqlite3'), global_burst=10, sender_burst=10)
    for launch in range(2):
        radio = ChannelRadio()
        radio.commands.names.update({2: '#traffic', 3: '#chess'})
        service = build_service(cfg, radio, EventLog(stream=io.StringIO()), references=())
        try:
            await service.start()
            await asyncio.wait_for(asyncio.gather(*(s._startup_announcement_task for s in service.services.values())), 1)
            sent = dict(radio.commands.sent)
            if launch == 0:
                assert set(sent) == {1, 2, 3}
                assert 'Mesh Potato Traffic v1.0:' in sent[2]
                assert 'Mesh Potato Chess v1.0:' in sent[3]
                assert f'v{__version__}:' in sent[1]
                assert all(len(('MeshAI: '+text).encode()) <= 160 for text in sent.values())
                await radio.deliver('Alice: version', 2)
                assert 'Traffic v1.0' in radio.commands.sent[-1][1]
                await radio.deliver('Alice: about', 3)
                assert 'Chess v1.0' in radio.commands.sent[-1][1]
            else:
                assert not sent
        finally:
            await service.stop()
        # Package and service version changes do not replay a welcome.
        monkeypatch.setattr('bot.channel_info.CHESS_VERSION', '1.1')
        monkeypatch.setattr('bot.service.__version__', '9.0.0')


async def test_ai_welcome_does_not_offer_traffic_when_redirecting(harness):
    h = harness(traffic_redirect=True, web_enabled=True)
    await h.service.start()
    await asyncio.wait_for(h.service._startup_announcement_task, 1)
    assert len(h.sent) == 1
    assert 'weather, sports, radio' in h.sent[0][1]
    assert 'traffic' not in h.sent[0][1]
    await h.service.stop()
