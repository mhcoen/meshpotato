"""Channel-utilization monitor: polls the radio's airtime and packet counters and scales
the global reply rate down when the channel is busy.

Signals, all from the companion via the library:
  get_stats_radio   -> tx_air_secs, rx_air_secs (cumulative, integer seconds), noise_floor,
                       last_rssi, last_snr
  get_stats_packets -> recv, sent, flood_rx, flood_tx, direct_rx, direct_tx (cumulative)

Duty cycle over the window = delta rx_air / elapsed: received airtime only. The bot's own
transmissions are exactly what the base rate limit governs, so counting them here would
make the limiter fight itself (one exchange looked like 20% over a fresh 10 s window).
Transmit airtime is still reported. Airtime is what this radio heard, so a busy channel
it cannot hear does not register; that is inherent to a node-local measurement. Counters
are integer seconds, so with a 60 s window the resolution is about 1.7 percentage points.
No level change is made until the window holds at least half of ``window_s`` of data.

Two policies run on the same level ladder, and the more restrictive one wins:
  received duty (other people's traffic, congestion):
    duty <  duty_low                -> "full"   factor 1.0
    duty_low <= duty < duty_high    -> "half"   factor 0.5
    duty >= duty_high               -> "paused" factor 0.0
  the bot's own transmit duty against tx_duty_budget (the courtesy dial):
    tx_duty <  budget               -> "full"
    budget <= tx_duty < 2 * budget  -> "half"
    tx_duty >= 2 * budget           -> "paused"
The counters are whole seconds, so over a window of W seconds the duty cycle moves in
steps of 1/W; with a 60 s window that is 1.7 points, and a threshold that sits on a step
flaps with every second of airtime. Two things keep the level steady: the window is
long (120 s by default), and a level only changes after consecutive polls agree.
Tightening needs TIGHTEN_POLLS polls in a row above a threshold; relaxing needs
RELAX_POLLS polls in a row below RELAX_MARGIN of the threshold that level guards, and
goes one level at a time.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from meshcore import EventType

from bot.jsonlog import EventLog
from bot.ratelimit import RateLimiter

LEVELS = ("full", "half", "paused")
FACTORS = {"full": 1.0, "half": 0.5, "paused": 0.0}
RELAX_MARGIN = 0.6  # relax only once duty is well under the threshold, not one second under it
TIGHTEN_POLLS = 2  # consecutive polls over a threshold before tightening
RELAX_POLLS = 3  # consecutive polls under the relax point before relaxing one level


@dataclass(frozen=True)
class Sample:
    t: float
    tx_air: int
    rx_air: int
    recv: int
    sent: int
    noise_floor: int | None
    last_rssi: int | None
    last_snr: float | None


@dataclass(frozen=True)
class Utilization:
    duty: float            # received airtime / elapsed
    tx_duty: float         # this radio's own transmit airtime / elapsed (reported, not acted on)
    packets_per_min: float
    window_s: float
    noise_floor: int | None
    last_rssi: int | None
    last_snr: float | None
    level: str
    factor: float
    reason: str = ""  # "rx" when received traffic set the level, "tx" when the bot's own budget did


def target_level(duty: float, duty_low: float, duty_high: float) -> str:
    return "paused" if duty >= duty_high else "half" if duty >= duty_low else "full"


def next_level(
    duty: float, current: str, duty_low: float, duty_high: float, over: int = TIGHTEN_POLLS, under: int = RELAX_POLLS
) -> str:
    """Pure policy step given the streak counts: ``over`` consecutive polls with the target
    above ``current``, ``under`` consecutive polls below the relax point of ``current``."""
    target = target_level(duty, duty_low, duty_high)
    cur_i, tgt_i = LEVELS.index(current), LEVELS.index(target)
    if tgt_i > cur_i:
        return target if over >= TIGHTEN_POLLS else current
    if tgt_i < cur_i:
        guard = duty_high if current == "paused" else duty_low
        if duty < guard * RELAX_MARGIN and under >= RELAX_POLLS:
            return LEVELS[cur_i - 1]
    return current


class Streaks:
    """Consecutive-poll counters for one policy (rx or tx)."""

    def __init__(self) -> None:
        self.over = 0
        self.under = 0
        self._target = "full"

    def update(self, duty: float, current: str, low: float, high: float) -> tuple[int, int]:
        target = target_level(duty, low, high)
        cur_i, tgt_i = LEVELS.index(current), LEVELS.index(target)
        self.over = (self.over + 1 if target == self._target else 1) if tgt_i > cur_i else 0
        self._target = target
        guard = high if current == "paused" else low
        self.under = self.under + 1 if (tgt_i < cur_i and duty < guard * RELAX_MARGIN) else 0
        return self.over, self.under


class UtilizationMonitor:
    def __init__(
        self,
        meshcore: Any,
        limiter: RateLimiter,
        log: EventLog,
        poll_s: float,
        window_s: float,
        duty_low: float,
        duty_high: float,
        clock: Callable[[], float] = time.monotonic,
        tx_budget: float = 1.0,
    ):
        self.mc = meshcore
        self.limiter = limiter
        self.log = log
        self.poll_s = poll_s
        self.window_s = window_s
        self.duty_low = duty_low
        self.duty_high = duty_high
        self.tx_budget = tx_budget
        self._clock = clock
        self._samples: deque[Sample] = deque()
        self._rx_streak = Streaks()
        self._tx_streak = Streaks()
        self.level = "full"
        self.current: Utilization | None = None
        self.polls = 0
        self.errors = 0
        self._consecutive_errors = 0
        self._task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="utilization-monitor")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
        self._set_level("full", duty=0.0)

    async def _run(self) -> None:
        while True:
            try:
                await self.sample()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - never let the monitor kill the bot
                self._sample_error(f"{type(exc).__name__}: {exc}")
            await asyncio.sleep(self.poll_s)

    # ------------------------------------------------------------------ sampling

    def _reset_window(self) -> None:
        self._samples.clear()
        self._rx_streak = Streaks()
        self._tx_streak = Streaks()

    def _sample_error(self, error: str, command: str = "") -> None:
        self._reset_window()
        self.errors += 1
        self._consecutive_errors += 1
        self.log.emit("utilization_error", command=command, error=error,
                      consecutive=self._consecutive_errors)
        if self._consecutive_errors >= 3:
            # Stale telemetry must not latch the channel off forever. Keep a
            # reduced rate until fresh samples establish a new policy window.
            self._set_level("half", duty=0.0, reason="stats-unavailable")
            self.current = None

    async def sample(self) -> Utilization | None:
        """Poll both counters once, update the window, apply the policy. Returns the reading."""
        self.polls += 1
        radio = await self.mc.commands.get_stats_radio()
        packets = await self.mc.commands.get_stats_packets()
        for name, res in (("get_stats_radio", radio), ("get_stats_packets", packets)):
            if res is None or res.type == EventType.ERROR:
                self._sample_error(str(getattr(res, "payload", None)), name)
                return self.current
        r, p = radio.payload or {}, packets.payload or {}
        sample = Sample(
            t=self._clock(),
            tx_air=int(r.get("tx_air_secs", 0)),
            rx_air=int(r.get("rx_air_secs", 0)),
            recv=int(p.get("recv", 0)),
            sent=int(p.get("sent", 0)),
            noise_floor=r.get("noise_floor"),
            last_rssi=r.get("last_rssi"),
            last_snr=r.get("last_snr"),
        )
        self._consecutive_errors = 0
        if self._samples and (
            sample.tx_air < self._samples[-1].tx_air
            or sample.rx_air < self._samples[-1].rx_air
            or sample.recv < self._samples[-1].recv
            or sample.sent < self._samples[-1].sent
            or sample.t - self._samples[-1].t > max(self.window_s, self.poll_s * 2)
        ):
            self._reset_window()
        self._samples.append(sample)
        # Keep the sample at/before the boundary, even when polling takes slightly
        # longer than a window. Gaps in collection are reset above.
        while len(self._samples) > 2 and sample.t - self._samples[1].t >= self.window_s:
            self._samples.popleft()

        oldest = self._samples[0]
        elapsed = sample.t - oldest.t
        if elapsed <= 0:
            return self.current  # first sample: nothing to compare yet
        duty = min(1.0, max(0.0, (sample.rx_air - oldest.rx_air) / elapsed))
        tx_duty = min(1.0, max(0.0, (sample.tx_air - oldest.tx_air) / elapsed))
        ppm = ((sample.recv - oldest.recv) + (sample.sent - oldest.sent)) / elapsed * 60.0

        reason = ""
        if elapsed >= self.window_s * 0.5:
            rx_over, rx_under = self._rx_streak.update(duty, self.level, self.duty_low, self.duty_high)
            tx_over, tx_under = self._tx_streak.update(tx_duty, self.level, self.tx_budget, 2.0 * self.tx_budget)
            rx_level = next_level(duty, self.level, self.duty_low, self.duty_high, rx_over, rx_under)
            tx_level = next_level(tx_duty, self.level, self.tx_budget, 2.0 * self.tx_budget, tx_over, tx_under)
            # Tighten if either policy says so; relax only when both agree the level can drop.
            cur = LEVELS.index(self.level)
            rx_i, tx_i = LEVELS.index(rx_level), LEVELS.index(tx_level)
            if max(rx_i, tx_i) > cur:
                level = LEVELS[max(rx_i, tx_i)]
                reason = "tx" if tx_i > rx_i else "rx"
            elif rx_i < cur and tx_i < cur:
                level = LEVELS[cur - 1]
                reason = "rx" if rx_i >= tx_i else "tx"
            else:
                level = self.level
                reason = "rx" if rx_i >= tx_i else "tx"
            if level != self.level:
                self._rx_streak = Streaks()
                self._tx_streak = Streaks()
        else:
            level = self.level  # too little data to act on yet
        changed = level != self.level
        self._set_level(level, duty if reason != "tx" else tx_duty, reason)
        self.current = Utilization(
            duty=duty,
            tx_duty=tx_duty,
            packets_per_min=ppm,
            window_s=elapsed,
            noise_floor=sample.noise_floor,
            last_rssi=sample.last_rssi,
            last_snr=sample.last_snr,
            level=level,
            factor=FACTORS[level],
            reason=reason,
        )
        self.log.emit(
            "utilization",
            duty=round(duty, 4),
            tx_duty=round(tx_duty, 4),
            tx_budget=self.tx_budget,
            reason=reason,
            packets_per_min=round(ppm, 2),
            window_s=round(elapsed, 1),
            noise_floor=sample.noise_floor,
            last_rssi=sample.last_rssi,
            last_snr=sample.last_snr,
            level=level,
            factor=FACTORS[level],
            level_changed=changed,
        )
        return self.current

    def _set_level(self, level: str, duty: float, reason: str = "") -> None:
        if level != self.level:
            self.log.emit("rate_level", old=self.level, new=level, duty=round(duty, 4), reason=reason)
        self.level = level
        self.limiter.set_global_factor(FACTORS[level])
