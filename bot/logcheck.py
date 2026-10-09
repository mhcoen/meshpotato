"""Read a JSON log and say what the radio heard but the bot never received.

An `rx` record is written for every packet the radio hears on the served channel
(when rx_log is on); a `received` record at ingestion and `inbound` for the final
decision. Legacy logs contain only `inbound`. A heard message with neither was lost between
the radio and the computer. The bot's own replies come back off the repeaters and
are heard too, so they are excluded.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path


def _norm(text: str) -> str:
    return " ".join(text.split())


@dataclass
class CheckResult:
    bot_name: str = ""
    heard: int = 0  # packets heard from others on the channel
    heard_own: int = 0  # the bot's own posts coming back off repeaters
    delivered: int = 0  # received messages, including waiting work; legacy inbound fallback
    decisions: Counter = field(default_factory=Counter)
    undelivered: list[tuple[str, str, int | None, float | None]] = field(default_factory=list)  # ts, text, rssi, snr
    rssi: list[int] = field(default_factory=list)
    snr: list[float] = field(default_factory=list)
    bad_lines: int = 0

    def render(self) -> str:
        lines = []
        if self.undelivered:
            lines.append("Heard by the radio but never delivered to the bot:")
            for ts, text, rssi, snr in self.undelivered:
                lines.append(f"  {ts}  rssi={rssi} snr={snr}  {text}")
        else:
            lines.append("Nothing heard by the radio went undelivered.")
        lines.append(
            f"{self.heard} heard from others on the channel, {self.heard_own} own replies heard back, "
            f"{self.delivered} delivered."
        )
        if self.decisions:
            parts = ", ".join(f"{k} {v}" for k, v in sorted(self.decisions.items()))
            lines.append(f"Decisions: {parts}.")
        if self.rssi:
            r = sorted(self.rssi)
            s = sorted(self.snr)
            lines.append(
                f"Heard signal: rssi min {r[0]} median {r[len(r) // 2]} max {r[-1]} dBm, "
                f"snr min {s[0]} median {s[len(s) // 2]} max {s[-1]} dB."
            )
        if self.bad_lines:
            lines.append(f"{self.bad_lines} unreadable line(s) skipped.")
        return "\n".join(lines)


def check_log(path: str | Path) -> CheckResult:
    result = CheckResult()
    heard: list[tuple[str, str, int | None, float | None]] = []
    heard_channels: list[int | None] = []
    delivered: set[tuple[int | None, str]] = set()
    pending: Counter[tuple[int | None, str]] = Counter()
    legacy_channel = None
    with Path(path).open(encoding="utf-8") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                r = json.loads(raw)
            except json.JSONDecodeError:
                result.bad_lines += 1
                continue
            event = r.get("event")
            channel = r.get("channel_idx", legacy_channel)
            if event == "startup" and r.get("bot_name"):
                result.bot_name = str(r["bot_name"])
                legacy_channel = channel
                # Cancelled work cannot finish after this channel restarts.
                pending = Counter({key: count for key, count in pending.items() if key[0] != channel})
            elif event == "rx" and r.get("ours") and r.get("message"):
                text = _norm(str(r["message"]))
                if result.bot_name and text.startswith(result.bot_name + ":"):
                    result.heard_own += 1
                    continue
                result.heard += 1
                heard.append((str(r.get("ts", "")), text, r.get("rssi"), r.get("snr")))
                heard_channels.append(channel)
                if isinstance(r.get("rssi"), (int, float)):
                    result.rssi.append(int(r["rssi"]))
                if isinstance(r.get("snr"), (int, float)):
                    result.snr.append(float(r["snr"]))
            elif event == "received":
                text = _norm(f"{r.get('sender', '')}: {r.get('prompt', '')}")
                result.delivered += 1
                pending[channel, text] += 1
                delivered.add((channel, text))
            elif event == "inbound":
                text = _norm(f"{r.get('sender', '')}: {r.get('prompt', '')}")
                if pending[channel, text]:
                    pending[channel, text] -= 1
                    if not pending[channel, text]:
                        del pending[channel, text]
                else:
                    result.delivered += 1
                result.decisions[str(r.get("decision", "?"))] += 1
                delivered.add((channel, text))
    result.undelivered = [h for channel, h in zip(heard_channels, heard) if (channel, h[1]) not in delivered]
    return result
