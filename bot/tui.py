"""Terminal monitor (Textual), running in the same process and event loop as the bot."""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual import events
from textual.containers import Horizontal
from textual.widgets import Footer, Header, RichLog, Static, Tab, Tabs
from rich.text import Text

from bot.config import Config
from bot.ratelimit import RateLimiter
from bot.service import Stats


class MeshPotatoApp(App[None]):
    TITLE = "Mesh Potato"
    CSS = """
    Horizontal#top { height: 14; }
    #status, #limits, #util { width: 1fr; border: round $primary; padding: 0 1; }
    #compact-status { height: auto; max-height: 5; padding: 0 1; }
    #channel-tabs { height: 3; }
    #channels { height: 1fr; min-height: 4; }
    .channel-log { width: 1fr; height: 1fr; border: round $secondary; }
    .channel-log.selected { border: round $primary; }
    #log { border: round $warning; height: 6; min-height: 3; }
    """
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("ctrl+c", "quit", "Quit", show=False),
        Binding("n", "next_channel", "Next channel"),
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
        channel_stats: dict[int, Stats] | None = None,
        reply_queue: Any = None,
    ):
        super().__init__()
        self._cfg = cfg
        self._stats = stats
        self._limiter = limiter
        self._monitor = monitor
        self._fortune = fortune
        self._channel_stats = channel_stats or {cfg.channel_idx: stats}
        self._active_slot = next((idx for idx, value in self._channel_stats.items() if value is stats), next(iter(self._channel_stats)))
        self._lines = {idx: deque(maxlen=2000) for idx in self._channel_stats}
        self._shared_lines = deque(maxlen=1000)
        self._layout_key = None
        self._reply_queue = reply_queue
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
        yield Static(id="compact-status")
        yield Tabs(*(Tab(Text(self._channel_label(idx)), id=f"tab-{idx}") for idx in self._channel_stats),
                   active=f"tab-{self._active_slot}", id="channel-tabs")
        with Horizontal(id="channels"):
            for idx in self._channel_stats:
                yield RichLog(id=f"channel-{idx}", classes="channel-log", min_width=1,
                              highlight=False, markup=False, wrap=True, max_lines=2000)
        yield RichLog(id="log", min_width=1, highlight=False, markup=False, wrap=True, max_lines=1000)
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

    def action_next_channel(self) -> None:
        slots = list(self._channel_stats)
        self._active_slot = slots[(slots.index(self._active_slot) + 1) % len(slots)]
        self._stats = self._channel_stats[self._active_slot]
        self.query_one('#channel-tabs', Tabs).active = f'tab-{self._active_slot}'
        self._refresh_panels()

    def on_tabs_tab_activated(self, event: Tabs.TabActivated) -> None:
        if event.tabs.id != 'channel-tabs':
            return
        self._active_slot = int(event.tab.id.removeprefix('tab-'))
        self._stats = self._channel_stats[self._active_slot]
        self._refresh_panels()

    def on_resize(self, event: events.Resize) -> None:
        if self.is_mounted:
            self.call_after_refresh(self._refresh_panels)

    def _channel_label(self, idx: int) -> str:
        name = self._channel_stats[idx].channel_name
        if idx == self._cfg.chess_channel_idx:
            label = 'Chess'
        elif idx == self._cfg.traffic_channel_idx:
            label = 'Traffic'
        elif name.lower().lstrip('#') == 'ai' or idx == self._cfg.channel_idx:
            label = 'AI'
        else:
            label = name or f'Channel {idx}'
        return f'{label} · {name}' if name and name.lower().lstrip('#') != label.lower().lstrip('#') else label

    def _layout_channels(self) -> None:
        width, height = self.size
        columns = len(self._channel_stats) == 1 or width >= 48*len(self._channel_stats)
        compact = width < 110 or height < 34
        self.query_one('#top').display = not compact
        self.query_one('#compact-status').display = compact
        self.query_one('#channel-tabs').display = not columns
        self.query_one('#log', RichLog).border_title = 'Shared radio events and errors'
        for idx in self._channel_stats:
            panel = self.query_one(f'#channel-{idx}', RichLog)
            panel.border_title = Text(self._channel_label(idx))
            panel.set_class(idx == self._active_slot, 'selected')
            panel.display = columns or idx == self._active_slot
            self.query_one(f'#tab-{idx}', Tab).label = Text(self._channel_label(idx))
        key = (width, height, columns, self._active_slot)
        if key != self._layout_key:
            self._layout_key = key
            self.call_after_refresh(self._redraw_logs)

    def _redraw_logs(self) -> None:
        for selector, lines in [(f'#channel-{idx}', lines) for idx, lines in self._lines.items()] + [('#log', self._shared_lines)]:
            panel = self.query_one(selector, RichLog)
            if panel.display:
                panel.clear()
                for line in lines:
                    panel.write(line)

    def _refresh_panels(self) -> None:
        s = self._stats
        cfg = self._cfg
        latency = f"{s.last_latency_ms:.0f} ms" if s.last_latency_ms is not None else "n/a"
        status = (
            f"[b]Radio[/b]   {'CONNECTED' if s.connected else 'DISCONNECTED'}  {cfg.port}\n"
            f"[b]Channel[/b] {s.channel_name or '?'} (idx {s.channel_idx})"
            f"{'  [n: next of ' + str(len(self._channel_stats)) + ']' if len(self._channel_stats) > 1 else ''}\n"
            f"[b]Persona[/b] {s.persona}"
            f"{'  until ' + time.strftime('%H:%M', time.localtime(s.persona_expires_at)) if s.persona_expires_at else ''}\n"
            f"[b]Model[/b]   {'Stockfish (chess)' if s.persona == 'chess' else 'Wisconsin 511 (traffic)' if s.persona == 'traffic' else cfg.backend + ':' + cfg.model}\n"
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
        queue_depth = len(self._reply_queue.waiting) if self._reply_queue is not None else s.queue_depth
        reply_active = self._reply_queue.active is not None if self._reply_queue is not None else s.reply_active
        limits = (
            f"[b]Rate limits[/b]\n"
            f"global  {snap['global_tokens']:.2f}/{snap['global_capacity']} tokens\n"
            f"        {snap['global_per_min']:g}/min effective "
            f"(configured {cfg.global_rate_per_min:g}/min x {snap['global_factor']:g})\n"
            f"per-sender {cfg.sender_rate_per_min:g}/min, recent:\n{senders}\n"
            f"shared queue {queue_depth}/{cfg.queue_max_pending}, active {'yes' if reply_active else 'no'}\n"
            f"expired {s.queue_expired}, queue-full {s.queue_full}"
        )
        self.query_one("#status", Static).update(status)
        self.query_one("#limits", Static).update(limits)
        self.query_one("#util", Static).update(self._utilization_text())
        errors = sum(s.send_errors+s.model_errors for s in self._channel_stats.values())
        connected = any(s.connected for s in self._channel_stats.values())
        self.query_one('#compact-status', Static).update(Text(
            f"Radio {'CONNECTED' if connected else 'DISCONNECTED'}  {cfg.port}\n"
            f"Queue {queue_depth}/{cfg.queue_max_pending}  active {'yes' if reply_active else 'no'}"
            f"  rate {snap['global_per_min']:g}/min  errors {errors}\n"
            f"Selected: {self._channel_label(self._active_slot)}  in {s.received}  replies {s.replies_sent}"
            f"  [n: next channel]"))
        self._layout_channels()

    def _fortune_text(self) -> str:
        f = self._fortune
        if len(self._channel_stats) > 1 and self._stats.channel_idx != self._cfg.channel_idx:
            return f"primary channel only (idx {self._cfg.channel_idx})"
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
                f"{ts} {record.get('sender', '?')}: {record.get('prompt', '')!s}\n"
                f"  {decision}{extra}"
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
        elif event in {'announce', 'post', 'traffic_alert_post'}:
            label = 'alert' if event == 'traffic_alert_post' else record.get('what', event)
            outcome = record.get('outcome', 'sent')
            line = f"{ts} [{label}: {outcome}] {record.get('text', '')}"
        elif event in (
            "startup", "shutdown", "connected", "disconnected", "send_error", "injection_block",
            "shutdown_error", "utilization_error", "reply_too_long", "persona_switch", "persona_reset",
            "announce", "announce_failed", "persona_timer_error", "fortune_scheduled", "fortune_posted",
            "fortune_deferred", "fortune_skipped", "fortune_error", "post", "post_error", "memory_forget",
            "queued", "dequeued", "reply_retry", "reply_rejected", "sports_lookup",
            "tip_scheduled", "tip_posted", "tip_skipped", "tip_error",
            "web_lookup", "web_source_rejected", "web_retry", "web_answer", "generation_budget_exhausted",
            "state_restored", "state_error", "chess_state_error",
            "traffic_alert_refresh", "traffic_alert_post", "traffic_alert_skipped", "traffic_alert_error",
        ):
            details = {k: v for k, v in record.items() if k not in ("ts", "event")}
            line = f"{ts} [{event}] {details}"
        else:
            return  # per-poll utilization records are shown in the panel, not the log
        if "channel_idx" in record:
            line = f"[ch {record['channel_idx']}] {line}"
        try:
            idx = record.get('channel_idx')
            channel_lines = getattr(self, '_lines', {})
            if idx is None and len(channel_lines) == 1:
                idx = next(iter(channel_lines))
            if idx in channel_lines:
                channel_lines[idx].append(line)
                self.query_one(f'#channel-{idx}', RichLog).write(line)
            # Unscoped events and all errors remain visible regardless of the selected tab.
            shared = (idx not in channel_lines or event in {'connected', 'disconnected', 'shutdown', 'rate_level'}
                      or 'error' in str(event) or str(event).endswith('_failed')
                      or record.get('outcome') in {'send-failed', 'state-error', 'unavailable'}
                      or event == 'inbound' and str(record.get('decision', '')).startswith('dropped:'))
            if shared:
                if hasattr(self, '_shared_lines'):
                    self._shared_lines.append(line)
                self.query_one("#log", RichLog).write(line)
        except Exception:  # noqa: BLE001 - widget may not be mounted yet
            pass
