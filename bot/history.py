"""Bounded, optionally age-limited channel history with timestamped snapshots."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from collections.abc import Callable, Iterable
import time


@dataclass(frozen=True)
class HistoryEntry:
    sender: str
    text: str
    flagged: bool = False  # the injection gate flagged this line at ingestion; never rendered
    score: float = 0.0
    rules: tuple[str, ...] = ()

    def line(self) -> str:
        return f"{self.sender}: {self.text}"


def render_transcript(lines: list[str], max_chars: int) -> str:
    """Join lines newest-last, dropping from the oldest end until within budget."""
    kept = list(lines)
    while kept and len("\n".join(kept)) > max_chars:
        kept.pop(0)
    return "\n".join(kept)


class History:
    def __init__(self, size: int, max_age_s: float | None = None, clock: Callable[[], float] = time.time):
        if size < 1:
            raise ValueError("history size must be at least 1")
        if max_age_s is not None and max_age_s <= 0:
            raise ValueError("history max age must be positive")
        self.max_age_s = max_age_s
        self._clock = clock
        self._buf: deque[tuple[float, HistoryEntry]] = deque(maxlen=size)

    def append(self, entry: HistoryEntry) -> None:
        self._buf.append((self._clock(), entry))

    def entries(self) -> list[HistoryEntry]:
        return [entry for _, entry in self.snapshot()]

    def snapshot(self) -> list[tuple[float, HistoryEntry]]:
        if self.max_age_s is not None:
            now = self._clock()
            self._buf = deque(((at, e) for at, e in self._buf if now - self.max_age_s <= at),
                              maxlen=self._buf.maxlen)
        return list(self._buf)

    def restore(self, rows: Iterable[tuple[float, HistoryEntry]]) -> None:
        now = self._clock()
        self._buf.clear()
        self._buf.extend((min(at, now), entry) for at, entry in rows)
        self.snapshot()

    def render(self, max_chars: int) -> str:
        lines = [e.line() for e in self.entries() if not e.flagged]
        return render_transcript(lines, max_chars)

    def clear(self) -> None:
        self._buf.clear()

    def __len__(self) -> int:
        return len(self.entries())
