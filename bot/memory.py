"""Per-person memory: the last few answered exchanges with each sender name.

Snapshots can be backed by SQLite. Sender names are unauthenticated, so this is
continuity for a conversation, not identity. Garbage collection has three parts:
a cap on rounds per person, an age limit on rounds, and a cap on the number of
people, least recently seen out first.
"""

from __future__ import annotations

import time
from collections import OrderedDict, deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace


@dataclass(frozen=True)
class Round:
    at: float
    prompt: str
    reply: str
    source_prompt: str | None = None  # original channel body, before trigger/sanitization


class PersonMemory:
    def __init__(
        self,
        rounds: int = 20,
        max_age_s: float = 7 * 24 * 3600.0,
        max_people: int = 500,
        clock: Callable[[], float] = time.monotonic,
    ):
        if rounds < 1 or max_people < 1 or max_age_s <= 0:
            raise ValueError("rounds and max_people must be at least 1, max_age_s positive")
        self.rounds = rounds
        self.max_age_s = max_age_s
        self.max_people = max_people
        self._clock = clock
        self._people: OrderedDict[str, deque[Round]] = OrderedDict()

    def record(self, sender: str, prompt: str, reply: str, *, source_prompt: str | None = None) -> None:
        """Remember one answered exchange. Evicts the least recently seen person past the cap."""
        self.sweep()
        rounds = self._people.get(sender)
        if rounds is None:
            rounds = deque(maxlen=self.rounds)
            self._people[sender] = rounds
        else:
            self._people.move_to_end(sender)
        rounds.append(Round(self._clock(), prompt, reply, source_prompt))
        while len(self._people) > self.max_people:
            self._people.popitem(last=False)

    def rounds_for(self, sender: str) -> list[Round]:
        """Fresh rounds for one sender, oldest first. Stale rounds are dropped on the way."""
        self.sweep()
        rounds = self._people.get(sender)
        if rounds is None:
            return []
        self._people.move_to_end(sender)
        return list(rounds)

    def forget(self, sender: str) -> bool:
        return self._people.pop(sender, None) is not None

    def snapshot(self) -> list[tuple[str, list[Round]]]:
        """Oldest-used people first, without changing LRU order."""
        self.sweep()
        return [(sender, list(rounds)) for sender, rounds in self._people.items()]

    def restore(self, people: Iterable[tuple[str, list[Round]]]) -> None:
        """Keep LRU order and age limits; clamp future timestamps after clock rollback."""
        self._people.clear()
        now = self._clock()
        for sender, rounds in people:
            fresh = [replace(r, at=min(r.at, now)) for r in rounds if now - self.max_age_s <= r.at]
            if fresh:
                self._people[sender] = deque(fresh, maxlen=self.rounds)
        while len(self._people) > self.max_people:
            self._people.popitem(last=False)

    def sweep(self) -> int:
        """Drop stale rounds everywhere and people with none left. Returns people removed."""
        removed = 0
        for sender in list(self._people):
            self._expire(sender, self._people[sender])
            if not self._people[sender]:
                del self._people[sender]
                removed += 1
        return removed

    def _expire(self, sender: str, rounds: deque[Round]) -> None:
        cutoff = self._clock() - self.max_age_s
        # Clock rollback can put an older timestamp behind a newer one.
        fresh = [r for r in rounds if r.at >= cutoff]
        rounds.clear()
        rounds.extend(fresh)

    @property
    def people(self) -> int:
        return len(self._people)

    @property
    def total_rounds(self) -> int:
        return sum(len(r) for r in self._people.values())


def render_rounds(rounds: list[Round], max_chars: int) -> str:
    """'asked: ...' / 'replied: ...' pairs, oldest first, trimmed from the oldest end to fit."""
    return "\n".join(_round_text(r) for r in fitting_rounds(rounds, max_chars))


def _round_text(r: Round) -> str:
    return f"asked: {r.prompt}\nreplied: {r.reply}"


def fitting_rounds(rounds: list[Round], max_chars: int) -> list[Round]:
    """Select complete rounds before using them to deduplicate channel context."""
    kept = list(rounds)
    total = sum(len(_round_text(r)) + 1 for r in kept) - 1
    while kept and total > max_chars:
        total -= len(_round_text(kept.pop(0))) + 1
    return kept
