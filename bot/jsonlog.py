"""JSON-lines event log with in-process listeners (the TUI subscribes to it).

Records are plain dicts. Nothing here is ever given a secret: the API key lives only
inside the backend's HTTP client.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from datetime import datetime, timezone
from typing import IO, Any

Listener = Callable[[dict[str, Any]], None]


class ChannelLog:
    """Tag channel-local events without duplicating the shared stream/listeners."""

    def __init__(self, parent: EventLog, channel_idx: int):
        self.parent = parent
        self.channel_idx = channel_idx

    def emit(self, event: str, **fields: Any) -> dict[str, Any]:
        fields.setdefault("channel_idx", self.channel_idx)
        return self.parent.emit(event, **fields)


class EventLog:
    def __init__(self, stream: IO[str] | None = None, path: str | None = None):
        self._file: IO[str] | None = None
        if path:
            self._file = open(path, "a", encoding="utf-8")  # noqa: SIM115 - closed in close()
            self._stream: IO[str] = self._file
        else:
            self._stream = stream if stream is not None else sys.stderr
        self._listeners: list[Listener] = []
        self.records_written = 0

    def subscribe(self, listener: Listener) -> None:
        self._listeners.append(listener)

    def emit(self, event: str, **fields: Any) -> dict[str, Any]:
        record: dict[str, Any] = {"ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"), "event": event}
        record.update(fields)
        try:
            self._stream.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
            self._stream.flush()
            self.records_written += 1
        except (OSError, ValueError):
            pass  # a dead log stream must never take the bot down
        for listener in list(self._listeners):
            try:
                listener(record)
            except Exception:  # noqa: BLE001 - a UI bug must not affect the bot
                pass
        return record

    def close(self) -> None:
        if self._file is not None:
            try:
                self._file.close()
            finally:
                self._file = None
