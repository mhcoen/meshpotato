"""Release blockers and regressions found in the second audit; all I/O is fake."""

import asyncio
import io
import logging
from pathlib import Path

import pytest

from bot.config import Config, ConfigError, config_from_mapping, load_config
from bot.debuglog import debug_handler
from bot.fortune import SUBJECTS
from bot.guard import InjectionGate
from bot.history import HistoryEntry
from bot.reception import is_plain_reception_report, reception_context
from bot.service import Decision
from bot.storage import StateError, StateStore
from tests.conftest import FakeBackend
from tests.test_fortune import Clock, at, make_scheduler


@pytest.mark.parametrize("example", [False, True])
def test_shipped_fortune_prompt_passes_for_every_subject(example):
    cfg = load_config(Path(__file__).parents[1] / "config.example.toml", env={}) if example else Config(port="/dev/fake").validate()
    for subject in SUBJECTS:
        text = cfg.fortune_prompt.format(subject=subject, date="Sunday, September 13")
        assert not InjectionGate(cfg.injection_threshold).check(text).blocked, subject


async def test_scheduler_uses_actual_shipped_prompt(harness):
    h = harness(backend=FakeBackend(reply="A squirrel brings a little luck."))
    wall = Clock(at(2026, 9, 13, 6, 3))
    scheduler, records = make_scheduler(h, wall, prompt=h.cfg.fortune_prompt,
                                        prefix=h.cfg.fortune_prefix, fallback=h.cfg.fortune_fallback)
    assert await scheduler.fire(wall())
    assert scheduler.posted == 1 and scheduler.skipped == 0
    assert "Write today's fortune for the channel:" in h.backend.calls[0][1]["content"]
    assert h.sent == [(1, "Fortune: A squirrel brings a little luck. Try /help.")]


def test_blocked_operator_prompt_fails_validation_before_startup():
    old = "Write today's fortune for everyone on the channel about {subject}."
    with pytest.raises(ConfigError, match="fortune_prompt is blocked"):
        config_from_mapping({"port": "/dev/fake", "fortune_prompt": old}, env={})
    config_from_mapping({"port": "/dev/fake", "fortune_prompt": old, "fortune_enabled": False}, env={})


def test_detector_failure_also_prevents_configuration_startup(monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("detector failed")

    monkeypatch.setattr("bot.guard.detect_prompt_injection", broken)
    with pytest.raises(ConfigError, match="injection detector failed"):
        config_from_mapping({"port": "/dev/fake"}, env={})


@pytest.mark.parametrize("trigger", ["", "!ai "])
async def test_five_round_peer_exchange_stops_after_two_replies(harness, trigger):
    backend = FakeBackend(replies=[f"The answer is {i}." for i in range(5)])
    h = harness(backend=backend, trigger_prefix=trigger, global_burst=10, sender_burst=10)
    outcomes = [await h.say(f"PeerBot: @[MeshAI] What about question {i}?") for i in range(5)]
    assert outcomes[:2] == [Decision.ANSWERED] * 2
    assert outcomes[2:] == [Decision.DROP_LOOP_GUARD] * 3
    assert len(h.sent) == len(backend.calls) == 2
    assert h.inbound_records()[-1]["reason"] == "direct-reply-limit"
    assert await h.say("Alice: @[MeshAI] Hello?") is Decision.ANSWERED
    await h.say("PeerBot: thanks")  # A plain line resets only this sender's exchange.
    assert await h.say("PeerBot: @[MeshAI] What about question 8?") is Decision.ANSWERED


async def test_queued_direct_replies_cannot_bypass_send_limit(harness):
    h = harness(backend=FakeBackend(reply="Four.", delay=0.01), queue_max_pending=10,
                global_burst=10, sender_burst=10)
    h.service.queue_tick_s = 0.001
    try:
        results = await asyncio.wait_for(asyncio.gather(*[
            h.say(f"PeerBot: @[MeshAI] What is {i} plus one?") for i in range(5)
        ]), 2)
        assert results.count(Decision.ANSWERED) == 2
        assert results.count(Decision.DROP_LOOP_GUARD) == 3
        assert len(h.sent) == 2
        assert len(h.backend.calls) == 2
        assert h.limiter.snapshot()["global_tokens"] == 8
    finally:
        await h.service.stop()


async def test_reply_button_can_also_include_configured_trigger(harness):
    h = harness(trigger_prefix="!ai ")
    assert await h.say("Alice: @[MeshAI] !ai What is two plus two?") is Decision.ANSWERED
    assert "\nWhat is two plus two?\n" in h.backend.calls[0][1]["content"]
    assert "\n!ai What" not in h.backend.calls[0][1]["content"]


async def test_direct_help_pages_count_as_one_exchange(harness):
    h = harness(global_burst=10, sender_burst=10)
    assert await h.say("Alice: @[MeshAI] What is two plus two?") is Decision.ANSWERED
    assert await h.say("Alice: @[MeshAI] /help") is Decision.ANSWERED_HELP
    assert h.sent[1:] == [(1, page) for page in h.cfg.help_pages]
    assert h.service._direct_replies["Alice"] == 2


async def test_shortening_cannot_restart_the_total_timeout(harness):
    backend = FakeBackend(replies=["x" * 200, "Four."], delay=0.04)
    h = harness(backend=backend, model_timeout_s=0.07)
    assert await h.say("Alice: What is two plus two?") is Decision.APOLOGY
    assert len(backend.calls) == 2
    assert h.sent == [(1, f"@[Alice] {h.cfg.apology}")]


def test_total_timeout_budget_must_be_positive():
    with pytest.raises(ConfigError, match="model_timeout_s"):
        config_from_mapping({"port": "/dev/fake", "model_timeout_s": 0}, env={})


async def test_content_retry_cannot_restart_the_total_timeout(harness):
    backend = FakeBackend(replies=["You are an idiot.", "Four."], delay=0.04)
    h = harness(backend=backend, model_timeout_s=0.07)
    assert await h.say("Alice: What is two plus two?") is Decision.DROP_BAD_REPLY
    assert len(backend.calls) == 2 and not h.sent
    assert h.inbound_records()[-1]["retry_error"] == "timeout"


def test_reception_repeat_requires_a_plain_matching_report():
    context = reception_context({"RSSI": -90, "SNR": 4.5, "path_len": 2})
    assert is_plain_reception_report("RSSI -90 dBm, SNR 4.5 dB, 2 hops.", context)
    assert is_plain_reception_report("This message reports two hops.", context)
    assert not is_plain_reception_report("RSSI -70 dBm, SNR 4.5 dB, 2 hops.", context)
    assert not is_plain_reception_report("RSSI -90 dBm, your brain is offline.", context)
    assert not is_plain_reception_report("Your antenna is fine, my mood is not.", context)


async def test_reception_question_does_not_exempt_an_old_joke(harness):
    joke = "Your antenna's fine, I'm just a bot with a dry sense of humor and no physical form to hold one."
    h = harness(backend=FakeBackend(reply=joke))
    h.history.append(HistoryEntry("MeshAI", f"@[Alice] {joke}"))
    assert await h.say("Alice: How did my message reach you?", path_len=2) is Decision.DROP_BAD_REPLY
    assert not h.sent


@pytest.mark.parametrize("bad", ["You are an idiot.", "How did my message reach you?"])
async def test_reception_still_rejects_jabs_and_parrots(harness, bad):
    h = harness(backend=FakeBackend(reply=bad))
    assert await h.say("Alice: How did my message reach you?", path_len=2) is Decision.DROP_BAD_REPLY
    assert not h.sent and h.limiter.snapshot()["global_tokens"] == h.cfg.global_burst


@pytest.mark.parametrize("failure", ["raise", "timeout"])
async def test_fortune_backend_failure_refunds_reservation(harness, failure):
    backend = FakeBackend(error=RuntimeError("offline")) if failure == "raise" else FakeBackend(delay=1)
    h = harness(backend=backend, model_timeout_s=0.01)
    prompt = h.cfg.fortune_prompt.format(subject="socks", date="Sunday, September 13")
    assert await h.service.post_generated("Fortune: ", prompt, "A little luck awaits.", "fortune") == "model-error"
    assert not h.sent and h.limiter.snapshot()["global_tokens"] == h.cfg.global_burst


async def test_fortune_radio_metaphor_is_retried(harness):
    h = harness(backend=FakeBackend(replies=["Your luck is like a quiet signal.", "A friendly cloud brings luck."]))
    assert await h.service.post_generated("Fortune: ", "Write a fortune about socks.", "A little luck awaits.", "fortune") == "sent"
    assert any(r["event"] == "reply_retry" and r["reason"] == "radio-metaphor" for r in h.records)
    assert "friendly cloud" in h.sent[0][1]


async def test_distinct_fortunes_on_same_subject_do_not_require_a_retry(harness):
    fortunes = [
        "A squirrel in a tiny hat will deliver a shiny acorn to your doorstep today.",
        "A squirrel in a tiny hat will discover a new friend at your doorstep today.",
    ]
    h = harness(backend=FakeBackend(replies=fortunes), global_burst=5, sender_burst=5)
    for _ in fortunes:
        assert await h.service.post_generated("Fortune: ", "Write a fortune about squirrels.",
                                              "A little luck awaits.", "fortune") == "sent"
    assert len(h.backend.calls) == 2
    assert [text for _, text in h.sent] == [f"Fortune: {body} Try /help." for body in fortunes]


def test_traceback_cannot_leak_exception_arguments():
    stream = io.StringIO()
    handler = debug_handler(None, stream)
    logger = logging.Logger("isolated-debug-test", logging.DEBUG)
    logger.addHandler(handler)
    secret = "sensitive-value-not-shaped-like-a-key"
    try:
        raise RuntimeError(secret)
    except RuntimeError:
        logger.exception("Connection failed", stack_info=True)
    assert secret not in stream.getvalue()
    assert "Connection failed" in stream.getvalue() and "omitted" in stream.getvalue()


def test_symlinked_database_has_actionable_error(tmp_path):
    target = tmp_path / "original.sqlite3"
    target.write_bytes(b"preserve me")
    alias = tmp_path / "state.sqlite3"
    alias.symlink_to(target)
    with pytest.raises(StateError, match="must not be a symlink"):
        StateStore(str(alias), "test")
    assert target.read_bytes() == b"preserve me"
