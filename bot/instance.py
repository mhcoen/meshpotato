"""One radio bot per login user, across terminals, checkouts, and command names.

The flock is held until service cleanup finishes. A replacement waits for that
lock before opening the radio. Never unlink it: waiters must share one inode.
"""

from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import re
import stat
import sys
import time

import psutil


class InstanceError(RuntimeError):
    """Cannot establish that this is the only running bot."""


def is_bot_command(argv: list[str], cwd: Path) -> bool:
    """Recognize launch targets, never incidental words in another command.

    Validate console scripts against their entry point so e.g. an unrelated
    script named meshpotato is not enough. Module launches must resolve to our
    package.
    """
    if not argv or not re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", Path(argv[0]).name, re.IGNORECASE):
        return False
    args = argv[1:]
    while args and args[0] in ("-u", "-B", "-E", "-s", "-S", "-I", "-O", "-OO"):
        args = args[1:]
    if not args:
        return False
    if args[0] == "-m":
        if len(args) < 2 or args[1] not in ("bot", "bot.cli"):
            return False
        target = cwd / "bot" / "cli.py"
        marker = 'prog="meshpotato"'
        legacy_marker = 'prog="meshai"'
    else:
        if Path(args[0]).name not in ("meshpotato", "meshai"):
            return False
        target = cwd / args[0]
        marker = legacy_marker = "from bot.cli import main"
    # Inspection commands and stop controllers never transmit.
    if any(arg in ("--stop", "--check", "--help", "-h", "--version") for arg in args[1:]):
        return False
    try:
        with target.open(encoding="utf-8") as source:
            text = source.read(32768)
        return marker in text or legacy_marker in text
    except (OSError, UnicodeError):
        return False


class SingleInstance:
    def __init__(self, path: Path | None = None, *, grace_s: float = 15, kill_wait_s: float = 3):
        self.path = path or Path.home() / ".meshpotato" / "instance.lock"
        self.grace_s = grace_s
        self.kill_wait_s = kill_wait_s
        self._file = None
        self._owned = False

    def _registered(self, process: psutil.Process) -> bool:
        return any(Path(item.path) == self.path for item in process.open_files())

    def _owner(self) -> psutil.Process | None:
        self._file.seek(0)
        try:
            record = json.loads(self._file.read(4096))
            process = psutil.Process(record["pid"])
            if (process.pid != os.getpid() and process.uids().real == os.getuid()
                    and process.create_time() == record["created"] and self._registered(process)):
                return process
        except psutil.AccessDenied:
            # The flock still excludes us. Wait for voluntary release until the
            # existing deadline; never signal a process we cannot verify.
            return None
        except (ValueError, TypeError, KeyError, psutil.NoSuchProcess):
            pass
        return None

    def __enter__(self):
        try:
            # Resolve parent aliases (/tmp on macOS, symlinked home directories),
            # but never resolve the final component: O_NOFOLLOW must reject it.
            self.path = self.path.parent.resolve() / self.path.name
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            parent = self.path.parent.lstat()
            if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid() or parent.st_mode & 0o077:
                raise InstanceError(f"instance directory must be private to your user: {self.path.parent}")
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            self._file = os.fdopen(fd, "r+", encoding="utf-8")
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise InstanceError(f"instance lock must be a private regular file: {self.path}")
            deadline = time.monotonic() + self.grace_s + self.kill_wait_s + 2
            signalled: dict[psutil.Process, float] = {}
            killed: set[psutil.Process] = set()
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise InstanceError("another Mesh Potato instance still holds the radio lock; refusing to start")
                    owner = self._owner()
                    if owner is not None:
                        try:
                            if owner not in signalled:
                                print(f"Stopping existing Mesh Potato (PID {owner.pid})...", file=sys.stderr)
                                owner.terminate()
                                signalled[owner] = time.monotonic()
                            elif owner not in killed and time.monotonic() - signalled[owner] >= self.grace_s:
                                print(f"Mesh Potato PID {owner.pid} did not exit; killing it.", file=sys.stderr)
                                owner.kill()
                                killed.add(owner)
                        except psutil.NoSuchProcess:
                            pass
                    time.sleep(0.05)
            self._owned = True
            self._file.seek(0)
            self._file.truncate()
            json.dump({"pid": os.getpid(), "created": psutil.Process().create_time()}, self._file)
            self._file.flush()
            return self
        except BaseException:
            self.close()
            raise

    def _legacy_processes(self) -> list[psutil.Process]:
        found = []
        for process in psutil.process_iter():
            if process.pid == os.getpid():
                continue
            try:
                if process.uids().real != os.getuid():
                    continue
                argv = process.cmdline()
                # Avoid reading cwd/open files for unrelated programs.
                if not argv or not re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", Path(argv[0]).name, re.IGNORECASE):
                    continue
                if is_bot_command(argv, Path(process.cwd())) and not self._registered(process):
                    found.append(process)
            except psutil.NoSuchProcess:
                continue
            except psutil.AccessDenied as exc:
                raise InstanceError(f"cannot inspect process {process.pid}; cannot verify that other bots are stopped") from exc
        return found

    def stop_others(self) -> None:
        """Retire pre-lock versions, including the old meshai console script.

        Updated launchers open the lock before waiting for it. Excluding those
        waiters prevents a shutting-down bot from killing its replacement.
        """
        if not self._owned:
            raise InstanceError("must own the instance lock before stopping other bots")
        processes = self._legacy_processes()
        for process in processes:
            try:
                print(f"Stopping legacy Mesh Potato (PID {process.pid})...", file=sys.stderr)
                process.terminate()
            except psutil.NoSuchProcess:
                pass
        _, alive = psutil.wait_procs(processes, timeout=self.grace_s)
        for process in alive:
            try:
                print(f"Mesh Potato PID {process.pid} did not exit; killing it.", file=sys.stderr)
                process.kill()
            except psutil.NoSuchProcess:
                pass
        _, alive = psutil.wait_procs(alive, timeout=self.kill_wait_s)
        remaining = self._legacy_processes()
        if alive or remaining:
            pids = sorted({p.pid for p in alive + remaining})
            raise InstanceError(f"Mesh Potato processes still running: {pids}; check for an automatic restart service")

    def close(self) -> None:
        if self._file is not None:
            # Keep metadata until the next owner replaces it. The lock, not a
            # stale PID file, decides liveness, including after SIGKILL/crashes.
            self._file.close()
            self._file = None
        self._owned = False

    def __exit__(self, exc_type, exc, tb):
        try:
            self.stop_others()
        except Exception as cleanup_error:
            if exc is None:
                raise
            note = f"Mesh Potato shutdown check also failed: {type(cleanup_error).__name__}: {cleanup_error}"
            exc.add_note(note)
            print(note, file=sys.stderr)
        finally:
            self.close()
