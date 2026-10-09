"""Own one physical radio while routing configured slots to isolated services."""

from __future__ import annotations

import asyncio

from meshcore import EventType

from bot.lifecycle import close_step, disconnect
from bot.service import BotService, ChannelError


class MultiChannelService:
    def __init__(self, cfg, meshcore, services: dict[int, BotService], log):
        self.cfg = cfg
        self.mc = meshcore
        self.services = services
        self.log = log
        primary = services[cfg.channel_idx]
        self.stats = primary.stats
        self.channel_stats = {idx: service.stats for idx, service in services.items()}
        self.limiter = primary.limiter
        self.monitor = primary.monitor
        self.fortune = primary.fortune
        self.traffic = primary.traffic
        self.reply_queue = primary.reply_queue
        self.shutdown_timeout_s = primary.shutdown_timeout_s
        self._subs = []
        self._start_task = None
        self._stop_task = None
        self._started = False
        self._stopped = False

    async def start(self) -> None:
        if self._stopped:
            return
        if self._start_task is not None:
            await asyncio.shield(self._start_task)
            return
        if self._started:
            return
        self._start_task = asyncio.current_task()
        try:
            # A missing slot or incompatible database must fail before any
            # handler, introduction, scheduler, or radio fetching is enabled.
            secrets = set()
            for service in self.services.values():
                await service.prepare()
                # Configuring the same logical channel in two slots would send
                # duplicate answers. Compare full secrets, never their short hashes.
                identity = service.channel_identity
                if identity is None or len(identity) != 16:
                    raise ChannelError(f"channel {service.cfg.channel_idx} did not report a full key; cannot verify distinct channels")
                if identity in secrets:
                    raise ChannelError("configured slots contain the same channel key; use only one slot per channel")
                secrets.add(identity)
            for service in self.services.values():
                await service.start()
            if self.cfg.rx_log != "off":
                self.mc.set_decrypt_channel_logs(True)
                self._subs.append(self.mc.subscribe(EventType.RX_LOG_DATA, self._on_rx_log))
            await self.mc.start_auto_message_fetching()
            self._started = True
        finally:
            self._start_task = None

    async def _on_rx_log(self, event) -> None:
        payload = event.payload or {}
        matches = [service for service in self.services.values()
                   if payload.get("chan_hash") is not None and payload["chan_hash"] == service._channel_hash]
        # Short channel hashes can collide. A decoded name can disambiguate;
        # never copy one decrypted message into several channels' diagnostics.
        if payload.get("chan_name") or len(matches) > 1:
            matches = [service for service in matches if service.stats.channel_name == payload.get("chan_name")]
        if len(matches) == 1:
            await matches[0]._on_rx_log(event)
        elif self.cfg.rx_log == "all":
            self.log.emit("rx", ours=False, type=payload.get("payload_typename"),
                          route=payload.get("route_typename"), path_len=payload.get("path_len"),
                          rssi=payload.get("rssi"), snr=payload.get("snr"),
                          chan=payload.get("chan_name") or payload.get("chan_hash"), message=None, msg_hash=None)

    async def stop(self) -> None:
        if self._stop_task is None:
            self._stopped = True
            self._stop_task = asyncio.create_task(self._stop(), name="channels-shutdown")
        await asyncio.shield(self._stop_task)

    async def _stop(self) -> None:
        if self._start_task is not None:
            self._start_task.cancel()
            await asyncio.gather(self._start_task, return_exceptions=True)
        for sub in self._subs:
            try:
                self.mc.unsubscribe(sub)
            except Exception as exc:
                self.log.emit("shutdown_error", step="unsubscribe", error=str(exc))
        self._subs.clear()
        # Cancel all channels together, before any waiting request can take
        # over the shared queue. The primary closes shared background workers.
        results = await asyncio.gather(*(service.stop() for service in self.services.values()), return_exceptions=True)
        for idx, result in zip(self.services, results):
            if isinstance(result, BaseException):
                self.log.emit("shutdown_error", channel_idx=idx, step="channel.stop", error=str(result))
        await close_step(self.mc.stop_auto_message_fetching, self.shutdown_timeout_s, self.log)
        await disconnect(self.mc, self.shutdown_timeout_s, self.log)
