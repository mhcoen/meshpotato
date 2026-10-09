"""Multi-channel routing, shared radio ownership, and private channel state.

All radios, model backends, and background data sources here are fakes.
"""

import asyncio
import io
import json

import pytest
from meshcore import EventType
from meshcore.events import Event

from bot import cli
from bot.channels import MultiChannelService
from bot.config import ConfigError, config_from_mapping
from bot.jsonlog import EventLog
from bot.logcheck import check_log
from bot.service import ChannelError, Decision
from tests.conftest import FakeBackend, FakeCommands, FakeMeshCore, make_config
from tests.test_queue import until


class ChannelCommands(FakeCommands):
    def __init__(self):
        super().__init__()
        self.names = {1: "#one", 2: "#two", 3: "#three"}
        self.secrets = {idx: bytes([idx]) * 16 for idx in self.names}

    async def get_channel(self, channel_idx):
        return Event(EventType.CHANNEL_INFO, {
            "channel_idx": channel_idx, "channel_name": self.names.get(channel_idx, ""),
            "channel_secret": self.secrets.get(channel_idx), "channel_hash": str(channel_idx),
        })


class ChannelRadio(FakeMeshCore):
    def __init__(self):
        super().__init__()
        self.commands = ChannelCommands()
        self.starts = self.stops = self.closes = 0

    async def start_auto_message_fetching(self):
        self.starts += 1
        await super().start_auto_message_fetching()

    async def stop_auto_message_fetching(self):
        self.stops += 1
        await super().stop_auto_message_fetching()

    async def disconnect(self):
        self.closes += 1
        await super().disconnect()


@pytest.fixture
async def multi(monkeypatch):
    built = []

    def make(**kwargs):
        cfg = make_config(**{
            "additional_channels": [2, 3], "announce_startup": False,
            "tips_enabled": False, "fortune_enabled": False, "adaptive_enabled": False,
            "queue_max_pending": 10, "global_burst": 20, "sender_burst": 20, **kwargs,
        })
        backends = []

        def backend(_cfg):
            result = FakeBackend(reply="The answer is four.")
            backends.append(result)
            return result

        monkeypatch.setattr(cli, "make_backend", backend)
        radio = ChannelRadio()
        log = EventLog(stream=io.StringIO())
        records = []
        log.subscribe(records.append)
        service = cli.build_service(cfg, radio, log, references=())
        assert isinstance(service, MultiChannelService)
        for child in service.services.values():
            child.queue_tick_s = 0.001
        built.append(service)
        return service, radio, backends, records

    yield make
    for service in built:
        await service.stop()


def test_configuration_and_environment():
    cfg = config_from_mapping({"radio": {"port": "/dev/fake", "channel_idx": 1, "additional_channels": [2, 4]}}, env={})
    assert cfg.channel_indices == (1, 2, 4)
    assert cfg.for_channel(1).state_db == "meshpotato.sqlite3"
    extra = cfg.for_channel(4)
    assert extra.state_db == "meshpotato.channel-4.sqlite3"
    assert not extra.announce_startup and not extra.tips_enabled and not extra.fortune_enabled
    assert make_config().channel_indices == (1,)
    assert config_from_mapping({"port": "/dev/fake"}, env={"MESHPOTATO_ADDITIONAL_CHANNELS": "2, 3"}).channel_indices == (1, 2, 3)
    assert config_from_mapping({"port": "/dev/fake"}, env={"MESHPOTATO_ADDITIONAL_CHANNELS": ""}).channel_indices == (1,)


@pytest.mark.parametrize("slots", [[1], [2, 2], [-1], [256], [True], [2.1], ["2"], "2,", "2.5", {}])
def test_invalid_slots(slots):
    with pytest.raises(ConfigError):
        make_config(additional_channels=slots)


async def test_routing_context_and_one_radio_owner(multi):
    service, radio, backends, records = multi()
    await asyncio.gather(service.start(), service.start())
    await radio.deliver("Alice: Why are violet flowers purple?", 1)
    await radio.deliver("Alice: How does a rook move?", 2)
    await radio.deliver("Alice: /help", 3)
    await radio.deliver("Alice: /help", 99)
    assert [slot for slot, _ in radio.commands.sent] == [1, 2, 3]
    assert "violet flowers" not in json.dumps(backends[1].calls)
    assert service.services[1].memory.rounds_for("Alice")[0].prompt == "Why are violet flowers purple?"
    assert service.services[2].memory.rounds_for("Alice")[0].prompt == "How does a rook move?"
    assert [r["channel_idx"] for r in records if r["event"] == "received"] == [1, 2, 3]
    assert radio.starts == 1
    await asyncio.gather(service.stop(), service.stop())
    assert radio.stops == radio.closes == 1
    assert all(backend.closed for backend in backends)
    assert len(radio.unsubscribed) == len(radio.subscriptions)


async def test_persona_and_forget_are_channel_local(multi):
    service, radio, _, _ = multi()
    await service.start()
    for idx, child in service.services.items():
        child.memory.record("Alice", f"Question on slot {idx}", "Hello.")
    await radio.deliver("Alice: /funny", 2)
    assert service.services[2].active_persona == "funny"
    assert service.services[1].active_persona == service.cfg.default_persona
    await radio.deliver("Alice: /forget", 2)
    assert not service.services[2].memory.rounds_for("Alice")
    assert service.services[1].memory.rounds_for("Alice")


async def test_persistence_survives_multi_and_single_channel_restarts(multi, tmp_path, monkeypatch):
    path = str(tmp_path / "state.sqlite3")
    service, _, _, _ = multi(state_db=path)
    await service.start()
    for idx, child in service.services.items():
        child.memory.record("Alice", f"Question on channel {idx}", "Hello.")
    await service.stop()
    restored, _, _, _ = multi(state_db=path)
    await restored.start()
    for idx, child in restored.services.items():
        assert child.memory.rounds_for("Alice")[0].prompt == f"Question on channel {idx}"
    await restored.stop()
    monkeypatch.setattr(cli, "make_backend", lambda cfg: FakeBackend())
    single = cli.build_service(make_config(state_db=path, announce_startup=False, tips_enabled=False,
                                           fortune_enabled=False, adaptive_enabled=False),
                               ChannelRadio(), EventLog(stream=io.StringIO()), references=())
    try:
        await single.start()
        assert single.memory.rounds_for("Alice")[0].prompt == "Question on channel 1"
    finally:
        await single.stop()


@pytest.mark.parametrize("failure", ["empty", "duplicate", "state", "missing-key"])
async def test_preflight_failure_sends_nothing_and_closes_everything(multi, tmp_path, failure):
    service, radio, backends, _ = multi(state_db=str(tmp_path / "state.sqlite3"), announce_startup=True)
    if failure == "empty":
        radio.commands.names[3] = ""
    elif failure == "duplicate":
        radio.commands.secrets[3] = radio.commands.secrets[1]
    elif failure == "missing-key":
        radio.commands.secrets[3] = None
    else:
        # A state DB from a different channel must never be silently reused.
        old, _, _, _ = multi(state_db=str(tmp_path / "state.sqlite3"))
        await old.start()
        await old.stop()
        radio.commands.names[3] = "#changed"
    with pytest.raises(ChannelError):
        await service.start()
    await service.stop()
    assert not radio.commands.sent and radio.starts == 0
    assert all(backend.closed for backend in backends)
    assert radio.closes == 1


async def test_shared_fifo_bound_and_no_parallel_generation(multi):
    service, radio, backends, records = multi(queue_max_pending=1)
    entered, release = asyncio.Event(), asyncio.Event()
    original = backends[0].complete

    async def held(messages, **kwargs):
        entered.set()
        await release.wait()
        return await original(messages, **kwargs)

    backends[0].complete = held
    await service.start()
    first = asyncio.create_task(radio.deliver("Alice: What is two plus two?", 1))
    await asyncio.wait_for(entered.wait(), 1)
    second = asyncio.create_task(radio.deliver("Bob: /help", 2))
    await until(lambda: len(service.reply_queue.waiting) == 1)
    await radio.deliver("Carol: /help", 3)
    assert any(r.get("channel_idx") == 3 and r.get("decision") == Decision.DROP_QUEUE_FULL for r in records)
    assert not backends[1].calls and not radio.commands.sent
    release.set()
    await asyncio.wait_for(asyncio.gather(first, second), 1)
    assert [slot for slot, _ in radio.commands.sent] == [1, 2]
    assert not service.reply_queue.requests and not service.reply_queue.waiting


async def test_failed_send_is_not_retried_and_uses_shared_rate_budget(multi):
    service, radio, _, records = multi(global_burst=1, queue_max_pending=0)
    await service.start()
    radio.commands.send_result_type = EventType.ERROR
    await radio.deliver("Alice: /help", 1)
    radio.commands.send_result_type = EventType.OK
    await radio.deliver("Bob: /help", 2)
    assert len(radio.commands.sent) == 1
    assert any(r.get("decision") == Decision.DROP_SEND_FAILED for r in records)
    assert any(r.get("channel_idx") == 2 and r.get("decision") == Decision.DROP_RATE_LIMITED for r in records)


async def test_shutdown_cancels_all_channels_and_pending_work(multi):
    service, radio, backends, _ = multi()
    backends[0].delay = 60
    await service.start()
    first = asyncio.create_task(radio.deliver("Alice: What is two plus two?", 1))
    await until(lambda: bool(backends[0].calls))
    second = asyncio.create_task(radio.deliver("Bob: /help", 2))
    await until(lambda: bool(service.reply_queue.waiting))
    await asyncio.wait_for(service.stop(), 1)
    results = await asyncio.gather(first, second, return_exceptions=True)
    assert all(isinstance(result, asyncio.CancelledError) for result in results)
    assert not radio.commands.sent and not service.reply_queue.requests and not service.reply_queue.waiting


async def test_rx_records_are_not_duplicated(multi):
    service, radio, _, records = multi(rx_log="all")
    await service.start()
    await radio.deliver_rx_log(chan_hash="2", chan_name="#two", message="Alice: hello")
    await radio.deliver_rx_log(chan_hash="99", chan_name="#other", message="Private")
    rows = [r for r in records if r["event"] == "rx"]
    assert len(rows) == 2 and rows[0]["channel_idx"] == 2
    assert rows[1]["message"] is None and rows[1]["ours"] is False
    assert service.services[2].stats.rx_heard == 1 and service.services[1].stats.rx_heard == 0


async def test_shutdown_during_preflight_cancels_start_and_releases_radio(multi):
    service, radio, backends, _ = multi()
    entered = asyncio.Event()

    async def blocked(idx):
        entered.set()
        await asyncio.Event().wait()

    radio.commands.get_channel = blocked
    startup = asyncio.create_task(service.start())
    await asyncio.wait_for(entered.wait(), 1)
    await asyncio.wait_for(service.stop(), 1)
    assert startup.cancelled() and radio.closes == 1
    assert all(backend.closed for backend in backends)


async def test_unsubscribe_failure_still_closes_radio(multi):
    service, radio, backends, _ = multi()
    await service.start()

    def failed(sub):
        raise RuntimeError("broken subscription")

    radio.unsubscribe = failed
    await service.stop()
    assert radio.closes == 1 and all(backend.closed for backend in backends)


async def test_hash_collision_uses_decoded_channel_identity(multi):
    service, radio, _, records = multi()
    await service.start()
    service.services[2]._channel_hash = "1"
    await radio.deliver_rx_log(chan_hash="1", chan_name="#two", message="Alice: hello")
    rows = [r for r in records if r["event"] == "rx"]
    assert len(rows) == 1 and rows[0]["channel_idx"] == 2


async def test_shared_data_workers_and_primary_only_announcements(multi, monkeypatch):
    class Worker:
        def __init__(self, *args, **kwargs):
            self.starts = self.stops = 0

        def start(self):
            self.starts += 1

        async def stop(self):
            self.stops += 1

    monkeypatch.setattr(cli, "TrafficCache", Worker)
    monkeypatch.setattr(cli, "UtilizationMonitor", Worker)
    service, _, _, _ = multi(web_enabled=True, adaptive_enabled=True, tips_enabled=True, fortune_enabled=True)
    await service.start()
    assert service.services[1].tips and service.services[1].fortune
    for child in list(service.services.values())[1:]:
        assert child.traffic is service.traffic and child.monitor is service.monitor
        assert child.tips is None and child.fortune is None and not child.cfg.announce_startup
    assert service.traffic.starts == service.monitor.starts == 1
    await service.stop()
    assert service.traffic.stops == service.monitor.stops == 1


def test_logcheck_does_not_match_identical_messages_across_channels(tmp_path):
    rows = [
        {"event": "startup", "channel_idx": 1, "bot_name": "MeshAI"},
        {"event": "startup", "channel_idx": 2, "bot_name": "MeshAI"},
        {"event": "rx", "channel_idx": 1, "ours": True, "message": "Alice: hello"},
        {"event": "received", "channel_idx": 2, "sender": "Alice", "prompt": "hello"},
        {"event": "inbound", "channel_idx": 2, "sender": "Alice", "prompt": "hello", "decision": "answered"},
    ]
    path = tmp_path / "log.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in rows))
    result = check_log(path)
    assert result.delivered == 1 and len(result.undelivered) == 1


async def test_monitor_switches_channels_and_labels_log_entries(multi):
    from bot.tui import MeshPotatoApp
    from textual.widgets import Static

    service, _, _, _ = multi()
    app = MeshPotatoApp(service.cfg, service.stats, service.limiter, service.log.subscribe,
                        service.start, service.stop, channel_stats=service.channel_stats,
                        reply_queue=service.reply_queue)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("n")
        assert app._stats is service.services[2].stats
        assert "#two" in str(app.query_one("#status", Static).render())
