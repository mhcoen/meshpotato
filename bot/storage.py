"""Bounded SQLite conversation snapshots; no radio, model, or timer state.

Calls are synchronous and contain no await: snapshots and /forget cannot race.
SQLite lock waits are capped at 100 ms. A generation check prevents two running
bots from silently overwriting each other's snapshots in the same file.
"""

from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import asdict

from bot.history import History, HistoryEntry
from bot.memory import PersonMemory, Round


class StateError(RuntimeError):
    """The state file cannot be safely read or written."""


def _allowed(gate, text: str) -> bool:
    try:
        verdict = gate.check(text)
    except Exception as exc:
        raise StateError("cannot validate saved conversations: injection detector failed") from exc
    if getattr(verdict, "error", None):
        raise StateError("cannot validate saved conversations: injection detector failed")
    return not verdict.blocked


class StateStore:
    APPLICATION_ID = 0x4D505354  # MPST

    def __init__(self, path: str, scope: str):
        self._db: sqlite3.Connection | None = None
        self._generation = 0
        self._last_snapshot = None
        try:
            self._db = sqlite3.connect(path, timeout=0.1)
            db = self._db
            application = db.execute("PRAGMA application_id").fetchone()[0]
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if application == 0 and version == 0:
                if db.execute("SELECT 1 FROM sqlite_master WHERE type='table'").fetchone():
                    raise StateError("not a Mesh Potato state database")
                with db:
                    db.execute("BEGIN IMMEDIATE")
                    db.execute("CREATE TABLE metadata (scope TEXT NOT NULL, generation INTEGER NOT NULL)")
                    db.execute("INSERT INTO metadata VALUES (?, 0)", (scope,))
                    db.execute("CREATE TABLE history (position INTEGER PRIMARY KEY, at REAL, sender TEXT, text TEXT)")
                    db.execute("CREATE TABLE people (sender TEXT PRIMARY KEY, position INTEGER, rounds TEXT)")
                    db.execute(f"PRAGMA application_id={self.APPLICATION_ID}")
                    db.execute("PRAGMA user_version=1")
            elif application != self.APPLICATION_ID or version != 1:
                raise StateError("unsupported state database format")
            meta = db.execute("SELECT scope, generation FROM metadata").fetchall()
            if len(meta) != 1 or meta[0][0] != scope:
                raise StateError("state database belongs to a different bot/channel; use a separate state_db file")
            self._generation = meta[0][1]
        except (sqlite3.Error, OSError, ValueError, StateError) as exc:
            self.close()
            raise StateError(f"{path}: {exc}") from exc

    def load(self, history: History, memory: PersonMemory, gate) -> None:
        """Load bounded rows, re-check saved text, then apply age and size limits."""
        try:
            entries = []
            rows = self._db.execute(
                "SELECT at, sender, text FROM history ORDER BY position DESC LIMIT ?",
                (history._buf.maxlen,),
            ).fetchall()
            for at, sender, text in reversed(rows):
                if not isinstance(sender, str) or not isinstance(text, str) or not math.isfinite(at):
                    raise ValueError("invalid history row")
                entry = HistoryEntry(sender, text)
                if _allowed(gate, entry.line()):
                    entries.append((at, entry))
            people = []
            rows = self._db.execute(
                "SELECT sender, rounds FROM people ORDER BY position DESC LIMIT ?", (memory.max_people,),
            ).fetchall()
            for sender, encoded in reversed(rows):
                if not isinstance(sender, str):
                    raise ValueError("invalid sender")
                decoded = json.loads(encoded)
                if not isinstance(decoded, list):
                    raise ValueError("invalid rounds")
                rounds = []
                for item in decoded[-memory.rounds:]:
                    r = Round(**item)
                    if (not math.isfinite(r.at) or not isinstance(r.prompt, str)
                            or not isinstance(r.reply, str)
                            or (r.source_prompt is not None and not isinstance(r.source_prompt, str))):
                        raise ValueError("invalid round")
                    text = f"{sender}: {r.source_prompt or r.prompt}\nasked: {r.prompt}\nreplied: {r.reply}"
                    if _allowed(gate, text):
                        rounds.append(r)
                people.append((sender, rounds))
            history.restore(entries)
            memory.restore(people)
        except (sqlite3.Error, ValueError, TypeError) as exc:
            raise StateError(f"cannot restore conversation state: {exc}") from exc

    def save(self, history: History, memory: PersonMemory) -> None:
        entries = [(at, e.sender, e.text) for at, e in history.snapshot() if not e.flagged]
        people = memory.snapshot()
        snapshot = (entries, people)
        if snapshot == self._last_snapshot:
            return
        encoded_people = [(sender, json.dumps([asdict(r) for r in rounds], ensure_ascii=True))
                          for sender, rounds in people]
        try:
            with self._db:
                updated = self._db.execute(
                    "UPDATE metadata SET generation=generation+1 WHERE generation=?", (self._generation,),
                )
                if updated.rowcount != 1:
                    raise StateError("state changed by another bot; use a separate state_db file")
                self._db.execute("DELETE FROM history")
                self._db.executemany("INSERT INTO history VALUES (?, ?, ?, ?)",
                                     [(i, *row) for i, row in enumerate(entries)])
                self._db.execute("DELETE FROM people")
                self._db.executemany("INSERT INTO people VALUES (?, ?, ?)",
                                     [(sender, i, encoded) for i, (sender, encoded) in enumerate(encoded_people)])
            self._generation += 1
            self._last_snapshot = snapshot
        except sqlite3.Error as exc:
            raise StateError(f"cannot save conversation state: {exc}") from exc

    def close(self) -> None:
        if self._db is not None:
            try:
                self._db.close()
            finally:
                self._db = None
