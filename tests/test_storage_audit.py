"""Regression tests for storage clock rollback and checkpoint failure recovery."""

import asyncio
from unittest.mock import AsyncMock

from bot.history import History, HistoryEntry
from bot.memory import PersonMemory, Round
from bot.service import Decision
from tests.conftest import FakeClock
from tests.test_queue import until


def test_backward_wall_clock_step_keeps_live_history():
    clock = FakeClock()
    history = History(20, max_age_s=3600, clock=clock)
    history.append(HistoryEntry("Alice", "first"))
    clock.advance(1)
    history.append(HistoryEntry("Alice", "second"))
    clock.advance(-2)  # an NTP correction after sleep, or a manual clock change
    assert [e.text for e in history.entries()] == ["first", "second"]


async def test_periodic_saver_survives_an_unexpected_exception(harness, tmp_path, monkeypatch):
    h = harness(state_db=str(tmp_path / "state.sqlite3"), fortune_enabled=False, state_save_interval_s=0.01)
    h.service._announce_startup = AsyncMock()
    await h.service.start()
    store = h.service._state_store
    real_save = store.save
    calls = []

    def flaky(*args):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("unexpected failure inside save")
        return real_save(*args)

    monkeypatch.setattr(store, "save", flaky)
    try:
        await h.say("Alice: Hi")
        await until(lambda: len(calls) >= 1)
        await asyncio.sleep(0.05)
        assert not h.service._state_task.done()
        assert any(r["event"] == "state_error" for r in h.records)
        await until(lambda: len(calls) >= 2)
    finally:
        await h.service.stop()


def test_restore_clamps_future_timestamps_but_still_expires_old_context():
    clock = FakeClock()
    history = History(20, max_age_s=100, clock=clock)
    memory = PersonMemory(max_age_s=100, clock=clock)
    history.restore([(clock() - 101, HistoryEntry("Alice", "expired")),
                     (clock() + 2, HistoryEntry("Alice", "recent"))])
    memory.restore([("Alice", [Round(clock() - 101, "expired", "Old."),
                               Round(clock() + 2, "recent", "New.", "!ai recent")])])
    assert history.snapshot() == [(clock(), HistoryEntry("Alice", "recent"))]
    assert memory.rounds_for("Alice") == [Round(clock(), "recent", "New.", "!ai recent")]
    clock.advance(101)
    assert not history.entries() and not memory.rounds_for("Alice")


def test_rollback_does_not_hide_expired_rounds_behind_future_rounds():
    clock = FakeClock()
    memory = PersonMemory(max_age_s=100, clock=clock)
    memory.record("Alice", "first", "Fine.")
    clock.advance(-50)
    memory.record("Alice", "second", "Fine.")
    clock.advance(101)
    assert [r.prompt for r in memory.rounds_for("Alice")] == ["first"]


async def test_live_model_context_survives_wall_clock_rollback(harness):
    h = harness(global_burst=2, sender_burst=2)
    wall_clock = FakeClock()
    h.history._clock = wall_clock
    try:
        assert await h.say("Bob: My dog is named Poppy.") is Decision.ANSWERED
        wall_clock.advance(-2)
        assert await h.say("Alice: What did Bob say?") is Decision.ANSWERED
        assert "Bob: My dog is named Poppy." in h.backend.calls[-1][1]["content"]
    finally:
        await h.service.stop()


async def test_unexpected_save_failure_does_not_abort_shutdown(harness, tmp_path, monkeypatch):
    h = harness(state_db=str(tmp_path / "state.sqlite3"), fortune_enabled=False)
    h.service._announce_startup = AsyncMock()
    await h.service.start()
    store = h.service._state_store
    task = h.service._state_task

    def broken(*args):
        raise RuntimeError("unexpected checkpoint failure")

    monkeypatch.setattr(store, "save", broken)
    await h.service.stop()
    assert task.cancelled() and store._db is None
    assert h.service._state_store is None
    assert h.backend.closed and h.mc.disconnected
    assert any(r["event"] == "state_error" for r in h.records)
    assert h.records[-1]["event"] == "shutdown"
