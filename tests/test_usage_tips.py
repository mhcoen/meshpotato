"""Usage tips use reviewed text, fake clocks and fake radios; never live broadcasts."""
import asyncio
import random
from datetime import datetime, timedelta

import pytest

from bot.config import ConfigError
from bot.guard import InjectionGate
from bot.storage import StateError, StateStore
from bot.usage_tips import TIPS, UsageTipScheduler, examples
from tests.conftest import make_config
from tests.test_fortune import Clock


def scheduler(h, start=None):
    wall = Clock(start or datetime(2026, 10, 9, 10, 1))
    s = UsageTipScheduler(h.service, h.log, now=wall, rng=random.Random(42))
    s.tick_s = 30
    async def sleep(target):
        seconds = max(0, (target-wall()).total_seconds())
        wall.advance(seconds)
        h.clock.advance(seconds)
        await asyncio.sleep(0)
    s._sleep_until = sleep
    return s, wall


def test_catalog_is_large_unique_safe_and_fits_shipped_wire_budget():
    cfg = make_config(bot_name='Mesh Potato', reply_max_chars=147, web_enabled=True)
    candidates = examples(cfg)
    assert len(candidates) == len(TIPS) >= 60
    assert len({key for key, _ in candidates}) == len(TIPS)
    assert len({text for _, text in candidates}) == len(TIPS)
    gate = InjectionGate(cfg.injection_threshold)
    for _, text in candidates:
        assert text.isascii() and '\n' not in text
        assert len(text) <= 147
        assert len(('Mesh Potato: '+text).encode()) <= 160
        assert not gate.check(text).blocked, text
    for category in ('traffic', 'weather', 'sports'):
        assert sum(key.startswith(category) for key, _ in candidates) >= 5


def test_enabled_features_and_configured_syntax():
    cfg = make_config(web_enabled=False, trigger_prefix='!ai ', command_prefix='!')
    tips = dict(examples(cfg))
    assert not any(key.startswith(('weather', 'sports', 'traffic')) for key in tips)
    assert 'weather' not in tips['intro']
    assert '"!ai !roll 2 6"' in tips['dice']
    assert '"!ai What does SNR mean?"' in tips['radio-snr']
    assert all(len(text) <= cfg.reply_max_chars for text in tips.values())


async def test_tip_sends_one_reviewed_message_without_model(harness):
    h = harness(web_enabled=True)
    s, wall = scheduler(h)
    h.clock.advance(61)
    assert await s.fire(wall())
    assert len(h.sent) == 1 and h.sent[0][1] in dict(examples(h.cfg)).values()
    assert not h.backend.calls
    assert h.service.stats.posts_sent == 1
    assert any(r['event'] == 'tip_posted' for r in h.records)
    assert not await s.fire(wall())
    assert len(h.sent) == 1


async def test_quiet_channel_waits_then_posts(harness):
    h = harness()
    s, wall = scheduler(h)
    slot = wall()
    assert await s.fire(slot)
    assert (wall()-slot).total_seconds() == 60
    assert len(h.sent) == 1


async def test_busy_channel_skips_without_queuing_or_generating(harness):
    h = harness()
    s, wall = scheduler(h)
    h.limiter.set_global_factor(0)
    assert not await s.fire(wall())
    assert not h.sent and not h.backend.calls
    assert not h.service._waiting
    assert any(r['event'] == 'tip_skipped' and r['reason'] == 'busy-or-expired' for r in h.records)


async def test_expired_slot_never_catches_up(harness):
    h = harness()
    s, wall = scheduler(h)
    assert not await s.fire(wall()-timedelta(hours=1))
    assert not h.sent


async def test_pending_question_takes_priority(harness):
    h = harness()
    s, wall = scheduler(h)
    h.clock.advance(61)
    entered, release = asyncio.Event(), asyncio.Event()
    async def question():
        async with h.service._request('Michael'):
            entered.set()
            await release.wait()
    task = asyncio.create_task(question())
    await entered.wait()
    assert not h.service.usage_tip_ready()
    assert await h.service.post_usage_tip('Potato tip: Try weather.', lambda: True) == 'busy'
    release.set()
    await task


async def test_radio_send_failure_is_not_retried(harness):
    h = harness()
    s, wall = scheduler(h)
    h.clock.advance(61)
    attempts = []
    async def ambiguous(*args):
        attempts.append(args)
        raise TimeoutError('delivery unknown')
    h.mc.commands.send_chan_msg = ambiguous
    assert not await s.fire(wall())
    assert not await s.fire(wall())
    assert len(attempts) == 1 and len(s.used) == 1


async def test_rotation_persists_and_claim_survives_restart(harness, tmp_path):
    path = str(tmp_path/'state.sqlite3')
    h = harness(web_enabled=True)
    h.service._state_store = StateStore(path, 'test-channel')
    s, wall = scheduler(h)
    s.restore()
    h.clock.advance(61)
    assert await s.fire(wall())
    used, slot = list(s.used), s.last_slot
    h.service._state_store.close()
    h.service._state_store = StateStore(path, 'test-channel')
    restarted, _ = scheduler(h)
    restarted.restore()
    assert restarted.used == used and restarted.last_slot == slot
    assert not await restarted.fire(slot)
    wall.advance(8*3600)
    h.clock.advance(8*3600)
    restarted._now = wall
    assert await restarted.fire(wall())
    assert len(set(restarted.used)) == 2
    h.service._state_store.close()


async def test_no_repeated_examples_until_catalog_exhausted(harness):
    h = harness(web_enabled=True)
    s, wall = scheduler(h)
    for _ in range(len(TIPS)):
        h.clock.advance(8*3600)
        wall.advance(8*3600)
        assert await s.fire(wall())
    assert len(set(s.used)) == len(TIPS)
    recent = s.used[-10:]
    h.clock.advance(8*3600)
    wall.advance(8*3600)
    assert await s.fire(wall())
    assert s.used[-1] not in recent


async def test_state_failure_blocks_tip_before_send(harness):
    h = harness()
    class Broken:
        def save_usage_tips(self, *args):
            raise StateError('disk full')
    h.service._state_store = Broken()
    s, wall = scheduler(h)
    h.clock.advance(61)
    assert not await s.fire(wall())
    assert not h.sent


def test_tip_state_preserves_single_owner_generation(tmp_path):
    path = str(tmp_path/'state.sqlite3')
    first, second = StateStore(path,'scope'), StateStore(path,'scope')
    first.save_usage_tips('2026-10-09T10:00:00', ['intro'])
    with pytest.raises(StateError, match='another bot'):
        second.save_usage_tips('2026-10-09T18:00:00', ['weather-local'])
    assert first.load_usage_tips()['used'] == ['intro']
    first.close(); second.close()


async def test_default_schedule_has_two_daytime_slots_and_stops(harness):
    h = harness()
    s, wall = scheduler(h, datetime(2026,10,9,9,0))
    fired=[]
    async def fire(slot):
        s.last_slot=slot
        fired.append(slot)
        if len(fired)==3:
            raise asyncio.CancelledError
        return False
    s.fire=fire
    s.start()
    task=s._task
    await asyncio.gather(task,return_exceptions=True)
    assert [(t.day,t.hour) for t in fired] == [(9,10),(9,18),(10,10)]
    assert all(t.minute <= 5 for t in fired)
    await s.stop()
    assert s._task is None


async def test_restart_after_base_time_skips_that_slot(harness):
    h = harness()
    s,wall=scheduler(h,datetime(2026,10,9,10,2))
    async def first(slot):
        assert slot.hour == 18
        raise asyncio.CancelledError
    s.fire=first
    s.start()
    await asyncio.gather(s._task,return_exceptions=True)
    await s.stop()


@pytest.mark.parametrize('settings', [
    dict(tips_morning_time='oops'), dict(tips_evening_time='10:00'),
    dict(tips_morning_time='20:00'), dict(tips_jitter_min=-1),
    dict(tips_window_min=0), dict(tips_window_min=31), dict(tips_quiet_s=0),
])
def test_bad_configuration_rejected(settings):
    with pytest.raises(ConfigError):
        make_config(**settings)


async def test_new_channel_activity_resets_quiet_period(harness):
    h = harness()
    h.clock.advance(61)
    assert h.service.usage_tip_ready()
    await h.say('Michael: hello')
    assert not h.service.usage_tip_ready()
    h.clock.advance(61)
    assert h.service.usage_tip_ready()


async def test_token_limit_does_not_queue_a_tip(harness):
    h = harness()
    h.clock.advance(61)
    assert await h.service.post_usage_tip('Potato tip: Try weather.', lambda: True) == 'sent'
    h.clock.advance(61)
    reservation = h.limiter.reserve('Another sender')
    reservation.commit()
    assert await h.service.post_usage_tip('Potato tip: Try a poem.', lambda: True) == 'rate-limited'
    assert len(h.sent) == 1 and not h.service._waiting


async def test_service_lifecycle_restores_and_stops_tips(harness, tmp_path):
    h = harness(state_db=str(tmp_path/'state.sqlite3'),fortune_enabled=False)
    s, _ = scheduler(h)
    h.service.tips = s
    entered = asyncio.Event()
    async def wait_forever(target):
        entered.set()
        await asyncio.Event().wait()
    s._sleep_until = wait_forever
    await h.service.start()
    await asyncio.wait_for(entered.wait(),1)
    task=s._task
    assert h.service._state_store is not None
    await h.service.stop()
    assert task.done() and s._task is None
    assert not any(text.startswith('Potato tip:') for _,text in h.sent)


@pytest.mark.parametrize('enabled',[True,False])
async def test_cli_tip_toggle(enabled):
    import io
    from bot.cli import build_service
    from bot.jsonlog import EventLog
    from tests.conftest import FakeMeshCore
    service=build_service(make_config(tips_enabled=enabled,fortune_enabled=False,adaptive_enabled=False),
                          FakeMeshCore(),EventLog(stream=io.StringIO()),references=())
    assert (service.tips is not None) == enabled
    await service.stop()
