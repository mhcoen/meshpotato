"""The daily fortune: an unsolicited post a little after a set time each morning.

The next firing is computed from the machine's local wall clock, read as naive
local time: the configured time plus a fresh random offset each day, so it never
lands on the exact minute. The wait re-reads the clock every tick, so a DST change
overnight or a machine that slept still fires when the wall clock says so. At firing the fortune is generated
in the built-in funny voice on a random subject, with a kindness instruction, through the same path as a
reply: plain ASCII, the injection check, the length cap with word-budget retries,
a global limiter token. If the channel is paused or the model fails, it retries
every couple of minutes until a cutoff after the scheduled time, then skips the
day. A bot started after the base time does not post that day.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import Any

from bot.jsonlog import EventLog

SUBJECTS = (
    "coffee", "socks", "squirrels", "the moon", "antennas", "batteries", "a lost umbrella", "cheese curds",
    "a rubber duck", "Tuesday", "a stapler", "the noise floor", "clouds", "a very small hat", "soup",
    "a lighthouse", "pockets", "a bicycle bell", "toast", "the wind", "a spreadsheet", "a garden gnome",
    "geese", "a paper map", "the number seven", "a kazoo", "gravel", "a thermos", "ferns", "a doorbell",
    "pancakes", "a crossword", "the horizon", "a sock drawer", "moss", "a screen door", "a compass",
    "pickles", "a hammock", "a lawn chair", "a trombone", "fog", "a bird feeder", "a rain gauge",
    "a shopping cart", "a solar panel", "a mailbox", "chalk", "a lake", "a spare key",
)


def parse_hhmm(text: str) -> tuple[int, int]:
    """'06:00' -> (6, 0). Raises ValueError for anything else."""
    parts = text.strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"time must be HH:MM, got {text!r}")
    hour, minute = int(parts[0]), int(parts[1])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"time must be HH:MM, got {text!r}")
    return hour, minute


def format_date(when: datetime) -> str:
    """'Friday, September 4' with no zero padding on the day."""
    return f"{when:%A}, {when:%B} {when.day}"


def next_fire(now: datetime, hhmm: str, jitter_min: float, rng: random.Random) -> datetime:
    """The next slot: today's if today's base time has not yet passed, else tomorrow's.

    Eligibility is decided on the base time, not the jittered slot. Deciding on the
    jittered slot re-rolled the offset against a day that had already fired and posted
    again whenever the new offset landed later than the old one. The same rule means a
    bot started after the base time waits for tomorrow instead of risking a second post.
    """
    hour, minute = parse_hhmm(hhmm)
    for day in (0, 1, 2):
        base = (now + timedelta(days=day)).replace(hour=hour, minute=minute, second=0, microsecond=0)
        if base < now or (day == 0 and now.fold):
            continue
        midnight = (base + timedelta(days=1)).replace(hour=0, minute=0, fold=0)
        room = (midnight - base).total_seconds() - 0.000001
        offset = rng.uniform(0.0, min(min(max(0.0, jitter_min), 1440.0) * 60.0, room))
        return base + timedelta(seconds=min(offset, room))
    raise RuntimeError("unreachable")


class FortuneScheduler:
    def __init__(
        self,
        service: Any,
        log: EventLog,
        hhmm: str,
        jitter_min: float,
        cutoff_min: float,
        prefix: str,
        prompt: str,
        fallback: str,
        retry_s: float = 120.0,
        now: Callable[[], datetime] = datetime.now,
        rng: random.Random | None = None,
    ):
        self.service = service
        self.log = log
        self.hhmm = hhmm
        self.jitter_min = jitter_min
        self.cutoff_min = cutoff_min
        self.prefix = prefix
        self.prompt = prompt
        self.fallback = fallback
        self.retry_s = retry_s
        self._now = now
        self._rng = rng or random.Random()
        self.tick_s = 30.0  # how often the wait re-checks the clock (tests shrink it)
        self.next_at: datetime | None = None
        self.posted = 0
        self.skipped = 0
        self._task: asyncio.Task[None] | None = None
        self._last_day: date | None = None

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="fortune-scheduler")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None

    async def _run(self) -> None:
        while True:
            try:
                now = self._now()
                if self._last_day is not None and now.date() <= self._last_day:
                    now = datetime.combine(self._last_day + timedelta(days=1), datetime.min.time(), tzinfo=now.tzinfo)
                self.next_at = next_fire(now, self.hhmm, self.jitter_min, self._rng)
                self.log.emit("fortune_scheduled", at=self.next_at.isoformat(timespec="seconds"))
                await self._sleep_until(self.next_at)
                await self.fire(self.next_at)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - never take the bot down
                self.log.emit("fortune_error", error=f"{type(exc).__name__}: {exc}")
                await asyncio.sleep(self.tick_s)

    async def _sleep_until(self, target: datetime) -> None:
        while True:
            remaining = (target - self._now()).total_seconds()
            if remaining <= 0:
                return
            await asyncio.sleep(min(self.tick_s, remaining))

    # ------------------------------------------------------------------ firing

    async def fire(self, slot: datetime) -> bool:
        """Try to post until the cutoff; return True if a fortune went out."""
        if self._last_day is not None and slot.date() <= self._last_day:
            return False
        self._last_day = slot.date()
        midnight = (slot + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0, fold=0)
        deadline = min(slot + timedelta(minutes=min(self.cutoff_min, 1440)), midnight - timedelta(microseconds=1))

        def can_send() -> bool:
            now = self._now()
            return slot <= now <= deadline and now.date() == slot.date() and not now.fold

        attempts = 0
        while True:
            if not can_send():
                self.skipped += 1
                self.log.emit("fortune_skipped", reason="expired", attempts=attempts)
                return False
            attempts += 1
            subject = self._rng.choice(SUBJECTS)
            request = self.prompt.format(subject=subject, date=format_date(self._now()))
            outcome = await self.service.post_generated(self.prefix, request, self.fallback, what="fortune", can_send=can_send)
            if outcome == "sent":
                self.posted += 1
                self.log.emit("fortune_posted", attempts=attempts, subject=subject)
                return True
            if outcome not in ("rate-limited", "model-error") or self._now() >= deadline:
                self.skipped += 1
                self.log.emit("fortune_skipped", reason=outcome, attempts=attempts)
                return False
            self.log.emit("fortune_deferred", reason=outcome, attempts=attempts)
            await self._sleep_for(min(self.retry_s, (deadline - self._now()).total_seconds()))

    async def _sleep_for(self, seconds: float) -> None:
        end = self._now() + timedelta(seconds=seconds)
        await self._sleep_until(end)
