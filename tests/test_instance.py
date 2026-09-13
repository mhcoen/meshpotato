"""Use disposable child processes; never start a radio or inspect real bots."""

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import psutil
import pytest

from bot.instance import InstanceError, SingleInstance, is_bot_command


@pytest.fixture
def children():
    running = []

    def spawn(code, *args):
        child = subprocess.Popen([sys.executable, "-c", code, *map(str, args)],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        running.append(child)
        return child

    yield spawn
    for child in running:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=5)


def ready(child, path):
    deadline = time.monotonic() + 10
    while not path.exists():
        if child.poll() is not None:
            pytest.fail(f"child exited early: {child.communicate()}")
        assert time.monotonic() < deadline, "child did not become ready"
        time.sleep(0.02)


HOLDER = """
import signal, sys, time
from pathlib import Path
from bot.instance import SingleInstance
SingleInstance._legacy_processes = lambda self: []
stopping = False
def stop(*args):
    global stopping
    stopping = True
signal.signal(signal.SIGTERM, signal.SIG_IGN if sys.argv[4] == 'stuck' else stop)
with SingleInstance(Path(sys.argv[1]), grace_s=.2, kill_wait_s=1):
    Path(sys.argv[2]).touch()
    while not stopping:
        time.sleep(.01)
    time.sleep(.15)
    Path(sys.argv[3]).touch()
"""


@pytest.fixture
def isolated(monkeypatch):
    monkeypatch.setattr(SingleInstance, "_legacy_processes", lambda self: [])


def test_replacement_waits_for_old_cleanup_before_acquiring_lock(tmp_path, children, isolated):
    lock, started, cleaned = (tmp_path / name for name in ("lock", "ready", "cleaned"))
    child = children(HOLDER, lock, started, cleaned, "normal")
    ready(child, started)
    with SingleInstance(lock, grace_s=1, kill_wait_s=1):
        assert cleaned.exists()
        assert json.loads(lock.read_text())["pid"] == os.getpid()
    assert child.wait(timeout=3) == 0


def test_stuck_owner_is_killed_and_lock_recovered(tmp_path, children, isolated):
    lock, started, cleaned = (tmp_path / name for name in ("lock", "ready", "cleaned"))
    child = children(HOLDER, lock, started, cleaned, "stuck")
    ready(child, started)
    with SingleInstance(lock, grace_s=.1, kill_wait_s=1):
        assert not cleaned.exists()
    assert child.wait(timeout=3) == -signal.SIGKILL


def test_stale_pid_file_never_signals_unrelated_live_process(tmp_path, children, isolated):
    child = children("import time; time.sleep(60)")
    process = psutil.Process(child.pid)
    lock = tmp_path / "lock"
    lock.write_text(json.dumps({"pid": child.pid, "created": process.create_time()}))
    lock.chmod(0o600)
    inode = lock.stat().st_ino
    with SingleInstance(lock):
        assert child.poll() is None
    with SingleInstance(lock):
        assert lock.stat().st_ino == inode  # never replace the inode under waiters
        assert child.poll() is None


def test_unknown_lock_owner_fails_without_signalling_stale_pid(tmp_path, children, isolated):
    lock, started = tmp_path / "lock", tmp_path / "ready"
    child = children("""
import fcntl, os, sys, time
from pathlib import Path
f = open(sys.argv[1], 'w')
os.chmod(sys.argv[1], 0o600)
fcntl.flock(f, fcntl.LOCK_EX)
f.write('{"pid": 1, "created": 0}')
f.flush()
Path(sys.argv[2]).touch()
time.sleep(60)
""", lock, started)
    ready(child, started)
    with pytest.raises(InstanceError, match="still holds"):
        with SingleInstance(lock, grace_s=0, kill_wait_s=0):
            pytest.fail("must not acquire a held lock")
    assert child.poll() is None


@pytest.mark.parametrize("name", ["meshpotato", "meshai"])
def test_identifies_only_real_entry_points(tmp_path, name):
    script = tmp_path / name
    script.write_text("from bot.cli import main\n")
    assert is_bot_command(["python3.12", str(script), "--config", "config.toml"], tmp_path)
    assert is_bot_command(["python", "-u", name], tmp_path)
    assert is_bot_command(["/Frameworks/Python.app/Contents/MacOS/Python", name], tmp_path)
    assert not is_bot_command(["python", "-c", "print('meshpotato')", str(script)], tmp_path)
    assert not is_bot_command(["sh", str(script)], tmp_path)
    assert not is_bot_command(["python", "other.py", str(script)], tmp_path)
    for flag in ("--stop", "--check", "--version", "--help"):
        assert not is_bot_command(["python", str(script), flag], tmp_path)
    script.write_text("print('unrelated program')")
    assert not is_bot_command(["python", str(script)], tmp_path)


def test_module_launch_requires_our_package(tmp_path):
    argv = ["python", "-m", "bot", "--headless"]
    assert not is_bot_command(argv, tmp_path)
    (tmp_path / "bot").mkdir()
    (tmp_path / "bot" / "cli.py").write_text('parser = argparse.ArgumentParser(prog="meshpotato")')
    assert is_bot_command(argv, tmp_path)


@pytest.mark.parametrize("name", ["meshpotato", "meshai"])
@pytest.mark.parametrize("when", ["startup", "shutdown"])
def test_cleans_up_legacy_processes_at_both_boundaries(tmp_path, monkeypatch, name, when):
    script, started = tmp_path / name, tmp_path / "ready"
    script.write_text("# from bot.cli import main\nimport sys, time\nfrom pathlib import Path\n"
                      "Path(sys.argv[1]).touch()\ntime.sleep(60)\n")
    child = None
    monkeypatch.setattr(psutil, "process_iter", lambda: [psutil.Process(child.pid)] if child and child.poll() is None else [])
    try:
        with SingleInstance(tmp_path / "lock", grace_s=.2, kill_wait_s=1) as instance:
            child = subprocess.Popen([sys.executable, str(script), str(started)])
            ready(child, started)
            if when == "startup":
                instance.stop_others()
                assert child.wait(timeout=3) in (0, -signal.SIGTERM)  # psutil may already have reaped it
        assert child.wait(timeout=3) in (0, -signal.SIGTERM)
    finally:
        if child and child.poll() is None:
            child.kill()
            child.wait(timeout=3)


def test_shutdown_does_not_kill_registered_replacement(tmp_path, children, monkeypatch):
    # This waiter has a real entry-point name but holds an open lock descriptor.
    script, started = tmp_path / "meshpotato", tmp_path / "ready"
    script.write_text("# from bot.cli import main\nimport sys, time\n"
                      "f = open(sys.argv[1], 'r+')\nfrom pathlib import Path\n"
                      "Path(sys.argv[2]).touch()\ntime.sleep(60)\n")
    child = None
    monkeypatch.setattr(psutil, "process_iter", lambda: [psutil.Process(child.pid)] if child and child.poll() is None else [])
    try:
        with SingleInstance(tmp_path / "lock"):
            child = subprocess.Popen([sys.executable, str(script), str(tmp_path / "lock"), str(started)])
            ready(child, started)
        assert child.poll() is None
    finally:
        if child and child.poll() is None:
            child.kill()
            child.wait(timeout=3)


def test_stop_command_needs_no_config_and_stops_owner(tmp_path, children, isolated, monkeypatch, capsys):
    from bot import cli

    lock, started, cleaned = (tmp_path / name for name in ("lock", "ready", "cleaned"))
    child = children(HOLDER, lock, started, cleaned, "normal")
    ready(child, started)
    monkeypatch.setattr(cli, "SingleInstance", lambda: SingleInstance(lock, grace_s=1, kill_wait_s=1))
    monkeypatch.setattr(cli, "load_config", lambda *a: pytest.fail("--stop must not load config"))
    assert cli.main(["--stop", "--config", "missing.toml"]) == 0
    assert child.wait(timeout=3) == 0
    assert "no other bot instances remain" in capsys.readouterr().out
    assert cli.main(["--stop"]) == 0  # idempotent


def test_symlink_lock_is_rejected(tmp_path, isolated):
    target = tmp_path / "untouched"
    target.write_text("keep me")
    lock = tmp_path / "lock"
    lock.symlink_to(target)
    with pytest.raises(OSError):
        with SingleInstance(lock):
            pytest.fail("symlink accepted")
    assert target.read_text() == "keep me"


def test_simultaneous_launches_never_overlap(tmp_path, children):
    code = """
import os, signal, sys, time
from pathlib import Path
from bot.instance import SingleInstance
SingleInstance._legacy_processes = lambda self: []
stopping = False
def stop(*args):
    global stopping
    stopping = True
signal.signal(signal.SIGTERM, stop)
with SingleInstance(Path(sys.argv[1]), grace_s=1, kill_wait_s=1):
    active = Path(sys.argv[2])
    fd = os.open(active, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    end = time.monotonic() + .3
    while not stopping and time.monotonic() < end:
        time.sleep(.01)
    time.sleep(.05)
    active.unlink()
"""
    processes = [children(code, tmp_path / "lock", tmp_path / "active") for _ in range(4)]
    for child in processes:
        out, err = child.communicate(timeout=10)
        assert child.returncode == 0, (out, err)
    assert not (tmp_path / "active").exists()


@pytest.mark.parametrize("failure", [False, True])
def test_cli_keeps_lock_through_runner_cleanup(tmp_path, isolated, monkeypatch, failure):
    import fcntl
    from bot import cli
    from tests.conftest import make_config

    lock = tmp_path / "lock"
    monkeypatch.setattr(cli, "SingleInstance", lambda: SingleInstance(lock))
    monkeypatch.setattr(cli, "load_config", lambda *a: make_config())
    monkeypatch.setattr(cli, "checked_references", lambda *a: ())

    def runner(*args):
        with lock.open() as contender:
            with pytest.raises(BlockingIOError):
                fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if failure:
            raise RuntimeError("runner failed after cleanup")
        return 0

    monkeypatch.setattr(cli, "_main_run", runner)
    if failure:
        with pytest.raises(RuntimeError, match="runner failed"):
            cli.main([])
    else:
        assert cli.main([]) == 0
    with lock.open() as contender:
        fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_invalid_config_does_not_stop_healthy_bot(monkeypatch):
    from bot import cli
    from bot.config import ConfigError

    def invalid(*a):
        raise ConfigError("bad config")

    monkeypatch.setattr(cli, "load_config", invalid)
    monkeypatch.setattr(cli, "SingleInstance", lambda: pytest.fail("must validate config before replacing bot"))
    assert cli.main([]) == 1


@pytest.mark.parametrize("original", [None, RuntimeError("original failure"), KeyboardInterrupt()])
def test_shutdown_scan_failure_preserves_original_error_and_releases_lock(tmp_path, monkeypatch, capsys, original):
    import fcntl

    cleanup_error = InstanceError("cannot inspect process")

    def failed_scan(self):
        raise cleanup_error

    # No real process enumeration, even on the cleanup failure path.
    monkeypatch.setattr(SingleInstance, "_legacy_processes", failed_scan)
    lock = tmp_path / "lock"
    expected = original if original is not None else cleanup_error
    with pytest.raises(type(expected)) as caught:
        with SingleInstance(lock):
            if original is not None:
                raise original
    assert caught.value is expected
    if original is not None:
        assert "cannot inspect process" in original.__notes__[0]
        assert "shutdown check also failed" in capsys.readouterr().err
    with lock.open() as contender:
        fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
