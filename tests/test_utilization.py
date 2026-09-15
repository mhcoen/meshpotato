"""Channel-utilization monitor: policy with dwell, sampling, limiter coupling, error handling."""

import asyncio
import io

import pytest
from meshcore import EventType
from meshcore.events import Event

from bot.jsonlog import EventLog
from bot.ratelimit import RateLimiter
from bot.utilization import RELAX_POLLS, TIGHTEN_POLLS, UtilizationMonitor, next_level, target_level
from tests.conftest import FakeClock, FakeMeshCore
from tests.conftest import make_config

LOW, HIGH = 0.05, 0.15


# ----------------------------------------------------------------------------- policy


@pytest.mark.parametrize(
    "duty,expected",
    [(0.00, "full"), (0.049, "full"), (0.05, "half"), (0.10, "half"), (0.15, "paused"), (0.50, "paused")],
)
def test_target_level(duty, expected):
    assert target_level(duty, LOW, HIGH) == expected


def test_tightening_needs_consecutive_polls_over():
    assert next_level(0.10, "full", LOW, HIGH, over=1, under=0) == "full"
    assert next_level(0.10, "full", LOW, HIGH, over=TIGHTEN_POLLS, under=0) == "half"
    assert next_level(0.50, "full", LOW, HIGH, over=TIGHTEN_POLLS, under=0) == "paused"  # straight past half


def test_relaxing_needs_consecutive_polls_well_under_and_goes_one_step():
    # relax point for "half" is 60% of low = 3%
    assert next_level(0.04, "half", LOW, HIGH, over=0, under=RELAX_POLLS) == "half"  # under low but not under 3%
    assert next_level(0.02, "half", LOW, HIGH, over=0, under=RELAX_POLLS - 1) == "half"  # not long enough
    assert next_level(0.02, "half", LOW, HIGH, over=0, under=RELAX_POLLS) == "full"
    # relax point for "paused" is 60% of high = 9%; one step only
    assert next_level(0.00, "paused", LOW, HIGH, over=0, under=RELAX_POLLS) == "half"
    assert next_level(0.10, "paused", LOW, HIGH, over=0, under=RELAX_POLLS) == "paused"


# ----------------------------------------------------------------------------- sampling


class StatsMeshCore(FakeMeshCore):
    """FakeMeshCore plus scripted radio/packet counters."""

    def __init__(self):
        super().__init__()
        self.tx_air = 0
        self.rx_air = 0
        self.recv = 0
        self.sent = 0
        self.fail_radio = False
        self.commands.get_stats_radio = self._get_stats_radio
        self.commands.get_stats_packets = self._get_stats_packets

    async def _get_stats_radio(self):
        if self.fail_radio:
            return Event(EventType.ERROR, {"reason": "timeout"})
        return Event(
            EventType.STATS_RADIO,
            {"noise_floor": -110, "last_rssi": -85, "last_snr": 7.5, "tx_air_secs": self.tx_air, "rx_air_secs": self.rx_air},
        )

    async def _get_stats_packets(self):
        return Event(
            EventType.STATS_PACKETS,
            {"recv": self.recv, "sent": self.sent, "flood_tx": 0, "direct_tx": 0, "flood_rx": 0, "direct_rx": 0},
        )


def make_monitor(clock, window_s=20.0, poll_s=10.0, tx_budget=1.0):
    """Default window of 20 s means the policy acts once 10 s of data are in hand."""
    mc = StatsMeshCore()
    limiter = RateLimiter(4.0, 1, 4.0, 1, clock=clock)
    records = []
    log = EventLog(stream=io.StringIO())
    log.subscribe(records.append)
    mon = UtilizationMonitor(mc, limiter, log, poll_s=poll_s, window_s=window_s, duty_low=LOW, duty_high=HIGH,
                             clock=clock, tx_budget=tx_budget)
    return mc, limiter, mon, records


async def polls(mon, clock, n, step=10):
    """Advance and sample n times; return the last reading."""
    u = None
    for _ in range(n):
        clock.advance(step)
        u = await mon.sample()
    return u


@pytest.mark.parametrize("rx_seconds,tx_seconds,step,expected", [
    (1, 0, 15, "full"),     # 6.7% receive airtime, like the reported quiet-night log
    (3, 0, 20, "half"),     # the new 15% receive threshold
    (6, 0, 20, "paused"),   # the new 30% receive threshold
    (0, 1, 20, "paused"),   # own transmissions can still exceed the separate budget
])
async def test_shipped_thresholds_with_radio_counters(rx_seconds, tx_seconds, step, expected):
    cfg = make_config()
    clock = FakeClock()
    mc, limiter, mon, records = make_monitor(clock)
    mon.window_s = cfg.utilization_window_s
    mon.duty_low, mon.duty_high = cfg.duty_low, cfg.duty_high
    mon.tx_budget = cfg.tx_duty_budget
    await mon.sample()
    for _ in range(12):
        clock.advance(step)
        mc.rx_air += rx_seconds
        mc.tx_air += tx_seconds
        reading = await mon.sample()
    assert reading.level == expected
    if expected == "full":
        assert not [r for r in records if r["event"] == "rate_level"]


async def test_first_sample_has_nothing_to_compare():
    clock = FakeClock()
    mc, limiter, mon, records = make_monitor(clock)
    assert await mon.sample() is None
    assert mon.level == "full" and limiter.global_factor == 1.0


async def test_one_busy_poll_does_not_tighten_but_two_do():
    clock = FakeClock()
    mc, limiter, mon, records = make_monitor(clock)
    await mon.sample()
    clock.advance(10)
    mc.rx_air = 2  # 20% over 10 s, but only one poll says so
    u = await mon.sample()
    assert u.duty == pytest.approx(0.2) and u.level == "full"
    clock.advance(10)
    mc.rx_air = 4  # still 20% over the window
    u = await mon.sample()
    assert u.level == "paused" and u.reason == "rx" and limiter.global_factor == 0.0
    rec = [r for r in records if r["event"] == "utilization"][-1]
    assert rec["level_changed"] is True
    assert any(r["event"] == "rate_level" and (r["old"], r["new"]) == ("full", "paused") for r in records)


async def test_duty_fields_and_packets_per_minute():
    clock = FakeClock()
    mc, limiter, mon, records = make_monitor(clock)
    await mon.sample()
    clock.advance(10)
    mc.tx_air, mc.rx_air, mc.recv, mc.sent = 1, 2, 4, 1
    u = await mon.sample()
    assert u.duty == pytest.approx(0.2) and u.tx_duty == pytest.approx(0.1)
    assert u.packets_per_min == pytest.approx(30.0) and u.window_s == pytest.approx(10.0)
    assert (u.noise_floor, u.last_rssi, u.last_snr) == (-110, -85, 7.5)


async def test_own_transmissions_never_throttle_the_bot_when_no_budget():
    clock = FakeClock()
    mc, limiter, mon, records = make_monitor(clock)
    await mon.sample()
    for _ in range(3):
        clock.advance(10)
        mc.tx_air += 5  # our own TX only
        u = await mon.sample()
    assert u.duty == 0.0 and u.level == "full" and limiter.global_factor == 1.0


async def test_no_action_until_half_the_window_is_in_hand():
    clock = FakeClock()
    mc, limiter, mon, records = make_monitor(clock, window_s=60.0)  # acts only from 30 s of data
    await mon.sample()
    for i in range(1, 3):
        clock.advance(10)
        mc.rx_air = 2 * i  # 20% throughout
        u = await mon.sample()
        assert u.level == "full"  # 10 s, 20 s: too little data
    clock.advance(10)
    mc.rx_air = 6
    assert (await mon.sample()).level == "full"  # 30 s: first poll counted
    clock.advance(10)
    mc.rx_air = 8
    assert (await mon.sample()).level == "paused"  # second consecutive poll: act


async def test_a_single_second_blip_does_not_flap_the_level():
    """The counters are whole seconds; one second of airtime on a 50 s window is 2 points."""
    clock = FakeClock()
    mc, limiter, mon, records = make_monitor(clock, window_s=60.0)
    await mon.sample()
    # One second of received airtime every other poll: over the sliding window the count
    # alternates 2 s and 3 s, so duty alternates 4% and 6% around the 5% threshold, which is
    # exactly the flapping seen live. Consecutive polls never agree, so the level holds.
    levels = []
    for i in range(1, 13):
        if i % 2 == 0:
            mc.rx_air += 1
        clock.advance(10.01)  # real polls run a hair over 10 s, so six samples span just over 60 s and
        u = await mon.sample()  # the window holds five intervals: the live 2 s / 3 s alternation
        levels.append((round(u.duty, 3), u.level))
    assert {lvl for _d, lvl in levels} == {"full"}, levels
    assert any(d >= 0.05 for d, _l in levels) and any(d < 0.05 for d, _l in levels)  # it really did straddle
    assert not any(r["event"] == "rate_level" for r in records)


async def test_relaxes_one_level_per_dwell_with_hysteresis():
    clock = FakeClock()
    mc, limiter, mon, records = make_monitor(clock, window_s=10.0)
    await mon.sample()
    mc.rx_air = 2
    await polls(mon, clock, 1)
    mc.rx_air = 4
    await polls(mon, clock, 1)
    assert mon.level == "paused"
    # quiet now: relax needs RELAX_POLLS polls under 60% of the guard, one level at a time
    u = await polls(mon, clock, RELAX_POLLS - 1)
    assert u.level == "paused"
    u = await polls(mon, clock, 1)
    assert u.level == "half" and limiter.global_factor == 0.5
    u = await polls(mon, clock, RELAX_POLLS)
    assert u.level == "full" and limiter.global_factor == 1.0


async def test_paused_level_actually_stops_replies_and_resumes(clock):
    mc, limiter, mon, records = make_monitor(clock)
    assert limiter.allow("alice").allowed  # spend the single burst token
    await mon.sample()
    mc.rx_air = 3
    await polls(mon, clock, 1)
    mc.rx_air = 6
    await polls(mon, clock, 1)
    assert mon.level == "paused"
    limiter.allow("carol")  # a token refilled during the 20 s before the pause took effect; spend it
    clock.advance(600)
    assert not limiter.allow("bob").allowed  # no refill while paused, even after 10 minutes
    await mon.sample()  # after a gap longer than the window there is nothing to compare: stays paused
    assert mon.level == "paused"
    await polls(mon, clock, RELAX_POLLS)  # quiet polls on record: relax one step
    assert mon.level == "half"
    clock.advance(60)
    assert limiter.allow("bob").allowed  # half rate: 2/min, so a token well within 60 s


async def test_counter_reset_restarts_the_window():
    clock = FakeClock()
    mc, limiter, mon, records = make_monitor(clock)
    mc.rx_air = 100
    await mon.sample()
    clock.advance(10)
    mc.rx_air = 1  # radio rebooted; counters went backwards
    u = await mon.sample()
    assert u is None  # window restarted, nothing to compare yet
    assert mon.level == "full"


async def test_stats_error_keeps_last_level_and_logs():
    clock = FakeClock()
    mc, limiter, mon, records = make_monitor(clock)
    await mon.sample()
    mc.rx_air = 2
    await polls(mon, clock, 1)
    mc.rx_air = 4
    await polls(mon, clock, 1)
    assert mon.level == "paused"
    mc.fail_radio = True
    clock.advance(10)
    assert (await mon.sample()).level == "paused"  # unchanged
    assert mon.errors == 1
    assert [r for r in records if r["event"] == "utilization_error"][0]["command"] == "get_stats_radio"


async def test_exception_in_poll_does_not_kill_the_loop():
    clock = FakeClock()
    mc, limiter, mon, records = make_monitor(clock, poll_s=0.001)

    async def boom():
        raise RuntimeError("serial hiccup")

    mc.commands.get_stats_radio = boom
    mon.start()
    await asyncio.sleep(0.02)
    await mon.stop()
    assert mon.errors >= 1
    assert any(r["event"] == "utilization_error" and "serial hiccup" in r["error"] for r in records)
    assert mon.level == "full" and limiter.global_factor == 1.0  # stop() restores full rate


async def test_start_and_stop_are_idempotent():
    clock = FakeClock()
    _mc, _limiter, mon, _records = make_monitor(clock, poll_s=0.001)
    mon.start()
    task = mon._task
    mon.start()
    assert mon._task is task
    await mon.stop()
    await mon.stop()
    assert mon._task is None


# ----------------------------------------------------------------------------- the transmit budget


async def test_own_transmit_budget_halves_the_rate_when_exceeded_for_two_polls():
    clock = FakeClock()
    mc, limiter, mon, records = make_monitor(clock, tx_budget=0.1)
    await mon.sample()
    mc.tx_air = 1  # 1 s in 10 s = 10%: at the budget, under twice it
    await polls(mon, clock, 1)
    assert mon.level == "full"  # one poll is not enough
    mc.tx_air = 2  # 2 s in 20 s: still 10%
    u = await polls(mon, clock, 1)
    assert u.level == "half" and u.reason == "tx" and limiter.global_factor == 0.5
    assert [r for r in records if r["event"] == "rate_level"][-1]["reason"] == "tx"


async def test_own_transmit_at_twice_the_budget_pauses():
    clock = FakeClock()
    mc, limiter, mon, records = make_monitor(clock, tx_budget=0.05)
    await mon.sample()
    mc.tx_air = 2  # 10% = 2 x budget
    await polls(mon, clock, 1)
    mc.tx_air = 4
    u = await polls(mon, clock, 1)
    assert u.level == "paused" and u.reason == "tx" and limiter.global_factor == 0.0


async def test_the_more_restrictive_policy_wins_and_both_must_agree_to_relax():
    clock = FakeClock()
    mc, limiter, mon, records = make_monitor(clock, tx_budget=0.05, window_s=10.0)
    await mon.sample()
    for _ in range(2):
        mc.rx_air += 4  # 40% received: paused by rx
        mc.tx_air += 1  # 10% own: paused by tx too
        await polls(mon, clock, 1)
    assert mon.level == "paused"
    # rx goes quiet, tx stays at 10%: no relaxing while one policy still wants paused
    for _ in range(RELAX_POLLS + 1):
        mc.tx_air += 1
        await polls(mon, clock, 1)
    assert mon.level == "paused" and mon.current.reason == "tx"
    # both quiet: relax
    await polls(mon, clock, RELAX_POLLS)
    assert mon.level == "half"
