"""Render the real terminal UI with sample traffic, without a radio or model.

Run from the repository root: .venv/bin/python -m scripts.capture_tui
"""

import asyncio
from datetime import datetime
from types import SimpleNamespace

from bot import __version__
from bot.config import config_from_mapping
from bot.ratelimit import RateLimiter
from bot.service import Stats
from bot.tui import MeshPotatoApp
from bot.utilization import Utilization


async def capture() -> None:
    cfg = config_from_mapping({"port": "/dev/cu.usbserial-demo"}, env={})
    stats = Stats(connected=True, channel_name="#ai", channel_idx=1,
                  received=5, replies_sent=5, persona="serious", rx_heard=18,
                  people_remembered=2, rounds_remembered=3, last_latency_ms=1820)
    limiter = RateLimiter(cfg.global_rate_per_min, cfg.global_burst,
                          cfg.sender_rate_per_min, cfg.sender_burst)
    monitor = SimpleNamespace(level="full", polls=24, errors=0, current=Utilization(
        duty=0.025, tx_duty=2 / 120, packets_per_min=9, window_s=120,
        noise_floor=-117, last_rssi=-104, last_snr=-6.25, level="full", factor=1,
    ))
    fortune = SimpleNamespace(next_at=datetime(2026, 9, 10, 6, 7), posted=1, skipped=0)

    async def idle() -> None:
        pass

    app = MeshPotatoApp(cfg, stats, limiter, lambda callback: None, idle, idle,
                        monitor=monitor, fortune=fortune)
    app.sub_title = "Sample traffic"
    app.console.width = 180

    def record(second: str, event: str, **details) -> None:
        app._on_record({"ts": f"2026-09-09T14:{second}-05:00", "event": event, **details})

    async with app.run_test(size=(180, 34)) as pilot:
        await pilot.pause()
        record("00:00", "announce", text=(
            f"Mesh Potato v{__version__}, LLM: {cfg.model}, "
            "https://github.com/mhcoen/meshpotato Try /help."
        ))
        record("01:00", "rx", ours=True, rssi=-98, snr=3.5, path_len=2,
               message="Andy: /help")
        for minute, page in zip(("01:08", "01:38"), cfg.help_pages):
            record(minute, "rx", ours=True, rssi=-91, snr=8.25, path_len=1,
                   message=f"Mesh Potato: {page}")
        record("02:10", "inbound", sender="Andy", path_len=2,
               decision="persona-switched", prompt="/serious", persona="serious")
        record("02:45", "inbound", sender="Andy", path_len=2, decision="answered",
               prompt="What does spreading factor change?",
               reply="@[Andy] A higher spreading factor can decode weaker signals, but each packet takes longer to send.")
        record("03:15", "inbound", sender="Sam", path_len=3, decision="answered",
               prompt="How did my message reach you?",
               reply="@[Sam] Your delivered copy reports three hops; I cannot identify the repeaters from this event.")
        record("03:45", "inbound", sender="Andy", path_len=2, decision="answered",
               prompt="Would that help my weak link?",
               reply="@[Andy] A higher spreading factor may help, but both ends must match the mesh settings.")
        await pilot.pause()
        app.save_screenshot("tui.svg", path="docs")


if __name__ == "__main__":
    asyncio.run(capture())
