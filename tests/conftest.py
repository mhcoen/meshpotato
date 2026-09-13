"""Shared fakes: a MeshCore stand-in, a scripted backend, a controllable clock."""

from __future__ import annotations

import asyncio
import io
from collections.abc import Callable
from typing import Any

import pytest
from meshcore import EventType
from meshcore.events import Event

from bot.config import Config, config_from_mapping
from bot.guard import InjectionGate
from bot.history import History
from bot.jsonlog import EventLog
from bot.knowledge import load_references
from bot.ratelimit import RateLimiter
from bot.service import BotService


class FakeClock:
    def __init__(self, start: float = 1000.0):
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class FakeCommands:
    def __init__(self, channel_name: str = "#ai"):
        self.channel_name = channel_name
        self.sent: list[tuple[int, str]] = []
        self.send_result_type = EventType.OK
        self.raise_on_send: Exception | None = None

    async def get_channel(self, channel_idx: int) -> Event:
        payload = {
            "channel_idx": channel_idx,
            "channel_name": self.channel_name,
            "channel_secret": b"\0" * 16,
            "channel_hash": "98",
        }
        return Event(EventType.CHANNEL_INFO, payload, payload)

    async def send_chan_msg(self, chan: int, msg: str) -> Event:
        if self.raise_on_send is not None:
            raise self.raise_on_send
        self.sent.append((chan, msg))
        if self.send_result_type == EventType.ERROR:
            return Event(EventType.ERROR, {"reason": "radio said no"})
        return Event(EventType.OK, {})


class FakeMeshCore:
    """Just enough of meshcore.MeshCore for the service: subscribe/unsubscribe, commands, lifecycle."""

    def __init__(self, channel_name: str = "#ai"):
        self.commands = FakeCommands(channel_name)
        self.subscriptions: list[tuple[Any, Callable, dict | None]] = []
        self.unsubscribed: list[Any] = []
        self.auto_fetch: bool | None = None
        self.disconnected = False
        self.is_connected = True
        self.decrypt_channel_logs = False
        self.self_info = {"name": "MeshAI", "tx_power": 22, "max_tx_power": 22, "radio_freq": 910.525,
                          "radio_bw": 62.5, "radio_sf": 7, "radio_cr": 5}

    def subscribe(self, event_type, callback, attribute_filters=None):
        sub = (event_type, callback, attribute_filters)
        self.subscriptions.append(sub)
        return sub

    def unsubscribe(self, sub) -> None:
        self.unsubscribed.append(sub)

    async def start_auto_message_fetching(self) -> None:
        self.auto_fetch = True

    async def stop_auto_message_fetching(self) -> None:
        self.auto_fetch = False

    async def disconnect(self) -> None:
        self.disconnected = True
        self.is_connected = False

    def set_decrypt_channel_logs(self, value: bool) -> None:
        self.decrypt_channel_logs = value

    async def deliver_rx_log(self, **payload) -> None:
        """Dispatch an RX_LOG_DATA event the way the library would after parsing a heard packet."""
        event = Event(EventType.RX_LOG_DATA, payload, {})
        for event_type, callback, _filters in list(self.subscriptions):
            if event_type == EventType.RX_LOG_DATA:
                await callback(event)

    async def deliver(self, text: str, channel_idx: int = 1, path_len: int = 0) -> None:
        """Dispatch a CHANNEL_MSG_RECV the way the library would, honouring attribute filters."""
        payload = {
            "type": "CHAN",
            "channel_idx": channel_idx,
            "path_len": path_len,
            "path_hash_mode": 0,
            "txt_type": 0,
            "sender_timestamp": 0,
            "text": text,
        }
        attributes = {"channel_idx": channel_idx, "txt_type": 0}
        event = Event(EventType.CHANNEL_MSG_RECV, payload, attributes)
        for event_type, callback, filters in list(self.subscriptions):
            if event_type != EventType.CHANNEL_MSG_RECV:
                continue
            if filters and any(attributes.get(k) != v for k, v in filters.items()):
                continue
            await callback(event)


class FakeBackend:
    name = "fake"

    def __init__(self, reply: str = "Four.", delay: float = 0.0, error: Exception | None = None, replies: list[str] | None = None):
        self.reply = reply
        self.replies = list(replies) if replies else None  # served in order; the last one repeats
        self.delay = delay
        self.error = error
        self.calls: list[list[dict[str, str]]] = []
        self.closed = False

    async def complete(self, messages: list[dict[str, str]]) -> str:
        self.calls.append(messages)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        if self.replies:
            return self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        return self.reply

    async def aclose(self) -> None:
        self.closed = True


def make_config(**overrides: Any) -> Config:
    # Legacy unit tests exercise immediate rate-limit decisions with a frozen
    # clock. Queue behavior/defaults are exercised explicitly in test_queue.py.
    # Keep the historical six-byte identity and 150-character edge cases explicit.
    # Current release defaults and the longer node name have dedicated tests.
    values: dict[str, Any] = {"port": "/dev/fake", "reply_delay_s": 0.0, "queue_max_pending": 0,
                              "bot_name": "MeshAI", "reply_max_chars": 150, "state_db": ""}
    values.update(overrides)
    return config_from_mapping(values, env={})


class Harness:
    def __init__(self, cfg: Config, backend: FakeBackend, clock: FakeClock, gate: Any = None, channel_name: str = "#ai"):
        self.cfg = cfg
        self.backend = backend
        self.clock = clock
        self.mc = FakeMeshCore(channel_name)
        self.mc.self_info["name"] = cfg.bot_name  # normal setup; mismatch tests override explicitly
        self.records: list[dict[str, Any]] = []
        self.log = EventLog(stream=io.StringIO())
        self.log.subscribe(self.records.append)
        self.gate = gate if gate is not None else InjectionGate(cfg.injection_threshold)
        self.limiter = RateLimiter(
            cfg.global_rate_per_min, cfg.global_burst, cfg.sender_rate_per_min, cfg.sender_burst, clock=clock
        )
        self.history = History(cfg.history_size)
        self.service = BotService(
            cfg=cfg,
            meshcore=self.mc,
            backend=backend,
            gate=self.gate,
            limiter=self.limiter,
            history=self.history,
            log=self.log,
            clock=clock,
            # Runtime detector fakes must not participate in CLI preflight.
            # Corpus startup validation has dedicated tests.
            references=load_references(),
        )

    @property
    def sent(self) -> list[tuple[int, str]]:
        return self.mc.commands.sent

    def inbound_records(self) -> list[dict[str, Any]]:
        return [r for r in self.records if r["event"] == "inbound"]

    async def say(self, text: str, path_len: int = 0, channel_idx: int | None = None):
        idx = self.cfg.channel_idx if channel_idx is None else channel_idx
        payload = {"channel_idx": idx, "path_len": path_len, "text": text}
        return await self.service.handle_payload(payload)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def harness(clock: FakeClock):
    def factory(backend: FakeBackend | None = None, gate: Any = None, channel_name: str = "#ai", **cfg_overrides: Any) -> Harness:
        cfg = make_config(**cfg_overrides)
        return Harness(cfg, backend or FakeBackend(), clock, gate=gate, channel_name=channel_name)

    return factory
