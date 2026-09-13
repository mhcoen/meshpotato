"""Command-line entry point: ``meshpotato --config config.toml [--headless]``."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from collections.abc import Sequence
from pathlib import Path

import psutil

from meshcore import EventType, MeshCore
from meshcore.serial_cx import SerialConnection
from serial import SerialException

from bot import __version__
from bot.backends import make_backend
from bot.config import Config, ConfigError, load_config
from bot.guard import InjectionGate
from bot.history import History
from bot.jsonlog import EventLog
from bot.knowledge import Reference, checked_references
from bot.logcheck import check_log
from bot.ratelimit import RateLimiter
from bot.service import BotService, ChannelError
from bot.fortune import FortuneScheduler
from bot.utilization import UtilizationMonitor
from bot.lifecycle import disconnect
from bot.instance import InstanceError, SingleInstance


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="meshpotato", description="MeshCore channel bot backed by a local LLM")
    parser.add_argument("--config", default="config.toml", help="path to the TOML config (default: config.toml)")
    parser.add_argument("--headless", action="store_true", help="no TUI; JSON log only")
    parser.add_argument("--log-file", default=None, help="override log_file from config")
    parser.add_argument(
        "--debug",
        action="store_true",
        help="also write meshcore's frame-level debug logging to <log file>.debug (or stderr when headless)",
    )
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--stop", action="store_true", help="stop all your Mesh Potato instances on this computer, then exit")
    actions.add_argument(
        "--check",
        metavar="LOG",
        help="read a JSON log and report anything the radio heard that the bot never received, then exit",
    )
    parser.add_argument("--version", action="version", version=f"meshpotato {__version__}")
    return parser


def build_service(cfg: Config, meshcore, log: EventLog, references: tuple[Reference, ...] | None = None) -> BotService:
    if references is None:
        references = checked_references(InjectionGate(cfg.injection_threshold))
    limiter = RateLimiter(
        global_per_min=cfg.global_rate_per_min,
        global_burst=cfg.global_burst,
        sender_per_min=cfg.sender_rate_per_min,
        sender_burst=cfg.sender_burst,
    )
    monitor = None
    if cfg.adaptive_enabled:
        monitor = UtilizationMonitor(
            meshcore=meshcore,
            limiter=limiter,
            log=log,
            poll_s=cfg.utilization_poll_s,
            window_s=cfg.utilization_window_s,
            duty_low=cfg.duty_low,
            duty_high=cfg.duty_high,
            tx_budget=cfg.tx_duty_budget,
        )
    service = BotService(
        cfg=cfg,
        meshcore=meshcore,
        backend=make_backend(cfg),
        gate=InjectionGate(threshold=cfg.injection_threshold),
        limiter=limiter,
        history=History(cfg.history_size),
        log=log,
        monitor=monitor,
        references=references,
    )
    if cfg.fortune_enabled:
        service.fortune = FortuneScheduler(
            service=service,
            log=log,
            hhmm=cfg.fortune_time,
            jitter_min=cfg.fortune_jitter_min,
            cutoff_min=cfg.fortune_cutoff_min,
            prefix=cfg.fortune_prefix,
            prompt=cfg.fortune_prompt,
            fallback=cfg.fortune_fallback,
        )
    return service


class ConnectError(RuntimeError):
    """The serial port could not be opened at all (wrong path, unplugged, permissions)."""


def _port_hint(port: str) -> str:
    """Name the serial ports that do exist, so a wrong `port` setting is a one line fix."""
    try:
        from serial.tools import list_ports

        found = [p.device for p in list_ports.comports() if "Bluetooth" not in p.device and "debug" not in p.device]
    except Exception:  # noqa: BLE001
        found = []
    if found:
        return "serial ports present: " + ", ".join(found) + ". Set `port` in config.toml (or MESHPOTATO_PORT) to the radio."
    return "no USB serial ports found. Is the radio plugged in? Check with: ls /dev/cu.* (macOS) or ls /dev/ttyUSB* /dev/ttyACM* (Linux)."


def release_boot_lines(meshcore) -> None:
    """Drop DTR and RTS after opening the port.

    pyserial asserts DTR on open and meshcore then clears RTS. On the ESP32
    auto-program circuit (CP2102 boards such as the Heltec Wireless Paper) that
    combination holds IO0 low for the whole session: the chip keeps running, but
    any reset while the port is open (brownout at full TX power, watchdog, crash)
    lands it in the serial bootloader instead of MeshCore, silently. With both
    lines released a reset boots MeshCore normally.
    """
    try:
        ser = meshcore.connection_manager.connection.transport.serial
        ser.dtr = False
        ser.rts = False
    except AttributeError:
        pass  # not a pyserial transport (tests, other connection types)


async def connect(cfg: Config, attempts: int = 3, boot_delay_s: float = 2.5):
    """Open the companion and complete the app-start handshake.

    Same steps as ``MeshCore.create_serial`` (dispatcher, port, app start), with two
    changes: the boot lines are released right after the port opens, and the
    handshake waits for a possible reboot and is retried without reopening the
    port, because reopening would toggle the lines again.
    """
    meshcore = MeshCore(SerialConnection(cfg.port, 115200))
    connected = False
    try:
        await meshcore.dispatcher.start()
        try:
            opened = await meshcore.connection_manager.connect()
        except (SerialException, OSError) as exc:
            raise ConnectError(f"cannot open {cfg.port}: {exc}\n{_port_hint(cfg.port)}") from None
        if opened is None:
            return None
        release_boot_lines(meshcore)
        await asyncio.sleep(boot_delay_s)
        for attempt in range(1, attempts + 1):
            res = await meshcore.commands.send_appstart()
            if res is not None and res.type != EventType.ERROR:
                connected = True
                return meshcore
            if attempt < attempts:
                print(f"no handshake from {cfg.port}, retrying ({attempt}/{attempts})...", file=sys.stderr)
        return None
    finally:
        if not connected:
            await disconnect(meshcore)


async def run(cfg: Config, headless: bool, log: EventLog, references: tuple[Reference, ...] | None = None) -> int:
    """Handle signals even during port opening and the boot delay."""
    loop = asyncio.get_running_loop()
    owner = asyncio.current_task()
    signalled = False

    def request_stop() -> None:
        nonlocal signalled
        if not signalled:
            signalled = True
            owner.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, request_stop)
    try:
        return await _run(cfg, headless, log, references)
    except asyncio.CancelledError:
        if signalled:
            return 0
        raise
    finally:
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(sig)


async def _run(cfg: Config, headless: bool, log: EventLog, references: tuple[Reference, ...] | None = None) -> int:
    if references is None:
        try:
            references = checked_references(InjectionGate(cfg.injection_threshold))
        except (ValueError, OSError) as exc:
            print(f"config error: radio references: {exc}", file=sys.stderr)
            return 1
    try:
        meshcore = await connect(cfg)
    except ConnectError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if meshcore is None:
        print(
            f"error: no response from a MeshCore companion on {cfg.port} "
            "(if it was running before, unplug and replug it: it may be stuck in the bootloader)",
            file=sys.stderr,
        )
        return 2
    service = None
    try:
        service = build_service(cfg, meshcore, log, references)
        return await _run_connected(cfg, service, headless, log)
    finally:
        if service is not None:
            await service.stop()
        else:
            await disconnect(meshcore, log=log)


async def _run_connected(cfg: Config, service: BotService, headless: bool, log: EventLog) -> int:
    loop = asyncio.get_running_loop()

    if headless:
        stop = asyncio.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        try:
            startup = asyncio.create_task(service.start())
            stopping = asyncio.create_task(stop.wait())
            try:
                await asyncio.wait((startup, stopping), return_when=asyncio.FIRST_COMPLETED)
                if stopping.done():
                    await service.stop()
                    return 0
                await startup
            finally:
                stopping.cancel()
                await asyncio.gather(stopping, return_exceptions=True)
        except ChannelError as exc:
            print(f"error: {exc}", file=sys.stderr)
            await service.stop()
            return 3
        await stop.wait()
        await service.stop()
        return 0

    from bot.tui import MeshPotatoApp  # imported lazily so headless runs need no terminal features

    async def run_service() -> None:
        await service.start()
        await asyncio.Event().wait()  # until quit or cancellation

    app = MeshPotatoApp(
        cfg=cfg,
        stats=service.stats,
        limiter=service.limiter,
        monitor=service.monitor,
        fortune=service.fortune,
        subscribe_log=log.subscribe,
        run_service=run_service,
        stop_service=service.stop,
    )
    loop.add_signal_handler(signal.SIGTERM, lambda: asyncio.ensure_future(app.action_quit()))
    await app.run_async()
    await service.stop()
    if app.exit_error:
        print(f"error: {app.exit_error}", file=sys.stderr)
        return 3
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if Path(sys.argv[0]).name == "meshai":
        print("The command is now meshpotato; meshai is a compatibility alias.", file=sys.stderr)
    if args.check:
        try:
            print(check_log(args.check).render())
        except FileNotFoundError:
            print(f"error: log file not found: {args.check}", file=sys.stderr)
            return 1
        return 0
    if args.stop:
        try:
            with SingleInstance() as instance:
                instance.stop_others()
            print("Mesh Potato stopped; no other bot instances remain.")
            return 0
        except (InstanceError, OSError, psutil.Error) as exc:
            print(f"process error: {exc}", file=sys.stderr)
            return 4
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 1
    try:
        references = checked_references(InjectionGate(cfg.injection_threshold))
    except (ValueError, OSError) as exc:
        print(f"config error: radio references: {exc}", file=sys.stderr)
        return 1
    try:
        with SingleInstance() as instance:
            instance.stop_others()
            return _main_run(args, cfg, references)
    except (InstanceError, OSError, psutil.Error) as exc:
        print(f"process error: {exc}", file=sys.stderr)
        return 4


def _main_run(args, cfg: Config, references: tuple[Reference, ...]) -> int:
    log_path = args.log_file if args.log_file is not None else (cfg.log_file or None)
    if log_path is None and not args.headless:
        log_path = "meshpotato.jsonl"  # the TUI owns the terminal, so stderr is not a usable log target
        print(f"JSON log: {log_path}", file=sys.stderr)
    if args.debug:
        # meshcore calls logging.basicConfig at import, so configure handlers explicitly.
        root = logging.getLogger()
        root.setLevel(logging.DEBUG)
        handler: logging.Handler = (
            logging.FileHandler(f"{log_path}.debug", encoding="utf-8") if log_path else logging.StreamHandler(sys.stderr)
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
        for existing in list(root.handlers):
            root.removeHandler(existing)
        root.addHandler(handler)
    log = EventLog(path=log_path)
    log.emit("process_start", pid=os.getpid(), config=str(Path(args.config).resolve()))
    try:
        return asyncio.run(run(cfg, headless=args.headless, log=log, references=references))
    finally:
        log.emit("process_stop", pid=os.getpid())
        log.close()


if __name__ == "__main__":
    sys.exit(main())
