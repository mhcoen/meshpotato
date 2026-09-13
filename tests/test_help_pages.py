"""Topic help sends one bounded, gated, rate-limited message per request."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from bot.guard import Verdict
from bot.service import Decision
from tests.conftest import make_config
from tests.test_queue import until


@pytest.mark.parametrize("topic", ["", "web", "voices", "fun", "privacy", "unknown", "fun extra", "VoIcEs"])
async def test_help_selects_one_display_without_model_or_lookup(harness, topic):
    h = harness(web_enabled=True)

    async def unexpected_lookup(*args, **kwargs):
        pytest.fail("help must not search the web")

    h.service.web.search = AsyncMock(side_effect=unexpected_lookup)
    assert await h.say(f"Alice: /help {topic}") is Decision.ANSWERED_HELP
    expected = {
        "": "Help: /help web | /help voices | /help fun | /help privacy",
        "web": "Ask here; I'll search the web when needed. /web <question> requests a search. Everything runs on my computer; no setup needed on yours.",
        "voices": "/nice /serious /funny /snarky /marvin /pirate /haiku",
        "fun": "/roll 3 8: roll 3 eight-sided dice (default 2 six-sided); /magic8 <question>: yes/no. Daily fortunes: funny and sweet.",
        "privacy": "/forget clears personal memory of you, not shared history; /reset restores the default voice for everyone.",
    }
    assert h.sent == [(1, expected.get(topic.lower(), expected[""]))]
    assert not h.backend.calls
    h.service.web.search.assert_not_awaited()
    assert h.limiter.snapshot()["global_tokens"] == 0
    assert h.inbound_records()[-1]["pages_sent"] == 1


async def test_help_reflects_disabled_features_and_custom_voices(harness):
    h = harness(web_enabled=False, fortune_enabled=False, personas={"nice": "Kind.", "spud": "Playful."},
                global_burst=3, sender_burst=3)
    for topic in ("web", "voices", "fun"):
        assert await h.say(f"Alice: /help {topic}") is Decision.ANSWERED_HELP
    assert h.sent[0][1] == "Web search is disabled."
    assert h.sent[1][1] == "/nice /spud"
    assert "fortune" not in h.sent[2][1].lower()
    assert not h.backend.calls


@pytest.mark.parametrize("topic", ["", "web", "voices", "fun", "privacy"])
async def test_help_obeys_trigger_and_custom_prefix(harness, topic):
    h = harness(trigger_prefix="!ai ", command_prefix="!", web_enabled=True)
    assert await h.say(f"Alice: !help {topic}") is Decision.DROP_NO_TRIGGER
    assert await h.say(f"Alice: !ai !help {topic}") is Decision.ANSWERED_HELP
    assert "!ai !" in h.sent[0][1]
    assert "/" not in h.sent[0][1].replace("yes/no", "")


@pytest.mark.parametrize("trigger,prefix", [("", "/"), ("!ai ", "/"), ("!ai ", "!!")])
def test_all_displays_fit_wire_and_pass_gate(trigger, prefix):
    from bot.guard import InjectionGate

    cfg = make_config(bot_name="Mesh Potato", reply_max_chars=147,
                      trigger_prefix=trigger, command_prefix=prefix, web_enabled=True)
    gate = InjectionGate(cfg.injection_threshold)
    for page in cfg.help_pages:
        assert len(page) <= cfg.reply_max_chars
        assert len(f"Mesh Potato: {page}".encode()) <= 160
        assert not gate.check(page).blocked


async def test_help_gate_block_spends_no_token(harness):
    class Gate:
        def check(self, text):
            return Verdict("Everything runs on my computer" in text, 1.0, (), text)

    h = harness(gate=Gate(), web_enabled=True)
    assert await h.say("Alice: /help web") is Decision.DROP_INJECTION
    assert not h.sent and not h.backend.calls
    assert h.limiter.snapshot()["global_tokens"] == 1


async def test_help_wait_expires_without_transmission(harness, clock):
    h = harness(queue_max_pending=10, queue_wait_s=10)
    h.service.queue_tick_s = 0.001
    h.limiter.set_global_factor(0)
    task = asyncio.create_task(h.say("Alice: /help fun"))
    try:
        await until(lambda: h.service.stats.queue_depth == 1)
        clock.advance(11)
        assert await asyncio.wait_for(task, 1) is Decision.DROP_QUEUE_EXPIRED
        assert not h.sent and not h.service._requests
    finally:
        await h.service.stop()


async def test_shutdown_cancels_waiting_help(harness):
    h = harness(queue_max_pending=10)
    h.limiter.set_global_factor(0)
    task = asyncio.create_task(h.say("Alice: /help"))
    await until(lambda: h.service.stats.queue_depth == 1)
    await h.service.stop()
    assert task.cancelled()
    assert not h.sent and not h.service._requests


async def test_help_send_failure_is_charged_once_without_retry(harness):
    h = harness(global_burst=2, sender_burst=2)
    h.mc.commands.raise_on_send = RuntimeError("radio disconnected")
    assert await h.say("Alice: /help") is Decision.DROP_SEND_FAILED
    assert not h.sent and not h.backend.calls
    assert h.service.stats.send_errors == 1
    assert h.inbound_records()[-1]["pages_sent"] == 0
    assert h.limiter.snapshot()["global_tokens"] == 1
