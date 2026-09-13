"""Terminal monitor (Textual), running in the same process and event loop as the bot."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import Footer, Header, RichLog, Static

from bot.config import Config
from bot.ratelimit import RateLimiter
from bot.service import Stats


class MeshPotatoApp(App[None]):
    TITLE = "Mesh Potato"
    CSS = """
    Horizontal#top { height: 14; }
    #status, #limits, #util { width: 1fr; border: round $primary; padding: 0 1; }
    #log { border: round $secondary; height: 1fr; }
    """
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("ctrl+c", "quit", "Quit", show=False),
    ]

    def __init__(
        self,
        cfg: Config,
        stats: Stats,
        limiter: RateLimiter,
        subscribe_log: Callable[[Callable[[dict[str, Any]], None]], None],
        run_service: Callable[[], Awaitable[None]],
        stop_service: Callable[[], Awaitable[None]],
        monitor: Any = None,
        fortune: Any = None,
    ):
        super().__init__()
        self._cfg = cfg
        self._stats = stats
        self._limiter = limiter
        self._monitor = monitor
        self._fortune = fortune
        self._subscribe_log = subscribe_log
        self._run_service = run_service
        self._stop_service = stop_service
        self._task: asyncio.Task[None] | None = None
        self.exit_error: str | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="top"):
            yield Static(id="status")
            yield Static(id="limits")
            yield Static(id="util")
        yield RichLog(id="log", highlight=False, markup=False, wrap=True, max_lines=2000)
        yield Footer()

    def on_mount(self) -> None:
        self._subscribe_log(self._on_record)
        self._task = asyncio.create_task(self._run())
        self.set_interval(1.0, self._refresh_panels)
        self._refresh_panels()

    async def _run(self) -> None:
        try:
            await self._run_service()
        except Exception as exc:  # noqa: BLE001
            self.exit_error = f"{type(exc).__name__}: {exc}"
            self.exit()

    async def action_quit(self) -> None:
        await self._stop_service()
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self.exit()

    # ------------------------------------------------------------------ rendering

    def _refresh_panels(self) -> None:
        s = self._stats
        cfg = self._cfg
        latency = f"{s.last_latency_ms:.0f} ms" if s.last_latency_ms is not None else "n/a"
        status = (
            f"[b]Radio[/b]   {'CONNECTED' if s.connected else 'DISCONNECTED'}  {cfg.port}\n"
            f"[b]Channel[/b] {s.channel_name or '?'} (idx {cfg.channel_idx})\n"
            f"[b]Persona[/b] {s.persona}"
            f"{'  until ' + time.strftime('%H:%M', time.localtime(s.persona_expires_at)) if s.persona_expires_at else ''}\n"
            f"[b]Model[/b]   {cfg.backend}:{cfg.model}\n"
            f"         last latency {latency}\n"
            f"[b]Counts[/b]  heard {s.rx_heard}  in {s.received}  replies {s.replies_sent}  apologies {s.apologies_sent}\n"
            f"         injection-blocked {s.injection_blocks}  rate-limited {s.rate_limited}\n"
            f"         send-err {s.send_errors}  model-err {s.model_errors}\n"
            f"         shorten-retries {s.shorten_retries}  too-long-fallbacks {s.fallbacks_sent}\n"
            f"[b]Fortune[/b] {self._fortune_text()}\n"
            f"[b]Memory[/b]  {s.people_remembered} people, {s.rounds_remembered} rounds"
        )
        snap = self._limiter.snapshot()
        senders = "\n".join(
            f"  {name[:20]:<20} {tokens:.2f}/{snap['sender_capacity']}" for name, tokens in snap["senders"].items()
        ) or "  (none yet)"
        limits = (
            f"[b]Rate limits[/b]\n"
            f"global  {snap['global_tokens']:.2f}/{snap['global_capacity']} tokens\n"
            f"        {snap['global_per_min']:g}/min effective "
            f"(configured {cfg.global_rate_per_min:g}/min x {snap['global_factor']:g})\n"
            f"per-sender {cfg.sender_rate_per_min:g}/min, recent:\n{senders}\n"
            f"queue {s.queue_depth}/{cfg.queue_max_pending}, active {'yes' if s.reply_active else 'no'}\n"
            f"expired {s.queue_expired}, queue-full {s.queue_full}"
        )
        self.query_one("#status", Static).update(status)
        self.query_one("#limits", Static).update(limits)
        self.query_one("#util", Static).update(self._utilization_text())

    def _fortune_text(self) -> str:
        f = self._fortune
        if f is None:
            return "off"
        nxt = f.next_at.strftime("%a %H:%M") if f.next_at else "?"
        return f"next {nxt}  posted {f.posted}  skipped {f.skipped}"

    def _utilization_text(self) -> str:
        cfg = self._cfg
        if self._monitor is None:
            return "[b]Channel utilization[/b]\n(adaptive limiting off)"
        m = self._monitor
        u = m.current
        head = (
            f"[b]Channel utilization[/b]  level {m.level.upper()}"
            f"{' (' + u.reason + ')' if u is not None and u.reason else ''}\n"
            f"own tx budget {cfg.tx_duty_budget:.1%} of channel time, "
            f"rx thresholds {cfg.duty_low:.0%} half / {cfg.duty_high:.0%} pause\n"
        )
        if u is None:
            return head + f"(warming up, polls {m.polls}, errors {m.errors})"
        return head + (
            f"rx duty     {u.duty:.1%} over {u.window_s:.0f}s   own tx {u.tx_duty:.1%} of {cfg.tx_duty_budget:.1%}\n"
            f"packets     {u.packets_per_min:.1f}/min\n"
            f"noise floor {u.noise_floor} dBm  rssi {u.last_rssi}  snr {u.last_snr}\n"
            f"polls {m.polls}  errors {m.errors}"
        )

    def _on_record(self, record: dict[str, Any]) -> None:
        event = record.get("event")
        ts = str(record.get("ts", ""))[11:19]
        if event == "inbound":
            decision = record.get("decision", "")
            extra = ""
            if decision == "dropped:injection-blocked":
                extra = f" [{record.get('point')} score={record.get('injection_score')} {record.get('injection_rules')}]"
            elif decision == "dropped:rate-limited":
                extra = f" [{record.get('reason')}]"
            elif decision == "dropped:bad-reply":
                extra = f" [{record.get('reason')}] {record.get('reply')}"
            elif decision == "persona-switched":
                extra = f" -> persona {record.get('persona')}"
            elif decision.startswith("answered") or decision == "apology":
                extra = f" -> {record.get('reply')}"
            line = (
                f"{ts} {record.get('sender', '?')!s:<16} hops={record.get('path_len')} "
                f"{decision:<24} {record.get('prompt', '')!s}{extra}"
            )
        elif event == "rx":
            if not record.get("ours"):
                return  # other channels' packets go to the JSON log only
            line = (
                f"{ts} [heard] rssi={record.get('rssi')} snr={record.get('snr')} hops={record.get('path_len')} "
                f"{record.get('message', '')!s}"
            )
        elif event == "rate_level":
            line = f"{ts} [rate] {record.get('old')} -> {record.get('new')} at duty {record.get('duty')} ({record.get('reason')})"
        elif event in (
            "startup", "shutdown", "connected", "disconnected", "send_error", "injection_block",
            "shutdown_error", "utilization_error", "reply_too_long", "persona_switch", "persona_reset",
            "announce", "announce_failed", "persona_timer_error", "fortune_scheduled", "fortune_posted",
            "fortune_deferred", "fortune_skipped", "fortune_error", "post", "post_error", "memory_forget",
            "queued", "dequeued", "reply_retry",
            "state_restored", "state_error",
        ):
            details = {k: v for k, v in record.items() if k not in ("ts", "event")}
            line = f"{ts} [{event}] {details}"
        else:
            return  # per-poll utilization records are shown in the panel, not the log
        try:
            self.query_one("#log", RichLog).write(line)
        except Exception:  # noqa: BLE001 - widget may not be mounted yet
            pass
