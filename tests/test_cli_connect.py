"""The connect sequence: open, release boot lines, wait, handshake with retries, no reopen."""

import pytest
from meshcore import EventType
from meshcore.events import Event

import bot.cli as cli
from tests.conftest import make_config


class _Serial:
    def __init__(self):
        self.dtr = True
        self.rts = False


class _Transport:
    def __init__(self):
        self.serial = _Serial()


class _Connection:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.transport = _Transport()


class _ConnectionManager:
    def __init__(self, connection, fail_open=False):
        self.connection = connection
        self.fail_open = fail_open
        self.connect_calls = 0

    async def connect(self):
        self.connect_calls += 1
        return None if self.fail_open else self.connection.args[0]


class _Dispatcher:
    def __init__(self):
        self.started = False

    async def start(self):
        self.started = True


class _Commands:
    def __init__(self, failures):
        self.failures = failures
        self.appstart_calls = 0

    async def send_appstart(self):
        self.appstart_calls += 1
        if self.appstart_calls <= self.failures:
            return Event(EventType.ERROR, {"reason": "timeout"})
        return Event(EventType.SELF_INFO, {"name": "MeshAI"})


class _MeshCore:
    instances: list["_MeshCore"] = []
    failures = 0
    fail_open = False

    def __init__(self, connection, **kwargs):
        self.dispatcher = _Dispatcher()
        self.connection_manager = _ConnectionManager(connection, fail_open=self.fail_open)
        self.commands = _Commands(self.failures)
        self.disconnected = False
        _MeshCore.instances.append(self)

    async def disconnect(self):
        self.disconnected = True


@pytest.fixture
def fake_library(monkeypatch):
    _MeshCore.instances = []
    _MeshCore.failures = 0
    _MeshCore.fail_open = False
    monkeypatch.setattr(cli, "MeshCore", _MeshCore)
    monkeypatch.setattr(cli, "SerialConnection", _Connection)

    async def no_sleep(_s):
        return None

    monkeypatch.setattr(cli.asyncio, "sleep", no_sleep)
    return _MeshCore


async def test_connect_releases_boot_lines_and_handshakes(fake_library):
    mc = await cli.connect(make_config(port="/dev/cu.test"))
    assert mc is not None
    assert mc.dispatcher.started is True
    assert mc.connection_manager.connection.args == ("/dev/cu.test", 115200)
    ser = mc.connection_manager.connection.transport.serial
    assert (ser.dtr, ser.rts) == (False, False)
    assert mc.commands.appstart_calls == 1
    assert mc.disconnected is False


async def test_handshake_is_retried_without_reopening_the_port(fake_library):
    fake_library.failures = 2
    mc = await cli.connect(make_config(port="/dev/cu.test"), attempts=3)
    assert mc is not None
    assert mc.commands.appstart_calls == 3
    assert mc.connection_manager.connect_calls == 1
    assert len(fake_library.instances) == 1


async def test_gives_up_and_disconnects_after_all_attempts(fake_library):
    fake_library.failures = 10
    mc = await cli.connect(make_config(port="/dev/cu.test"), attempts=3)
    assert mc is None
    inst = fake_library.instances[0]
    assert inst.commands.appstart_calls == 3
    assert inst.disconnected is True


async def test_port_open_failure_returns_none(fake_library):
    fake_library.fail_open = True
    assert await cli.connect(make_config(port="/dev/cu.test")) is None
    assert fake_library.instances[0].disconnected is True


def test_release_boot_lines_tolerates_other_transports():
    class Bare:
        connection_manager = object()

    cli.release_boot_lines(Bare())  # must not raise


async def test_service_construction_error_is_clean_and_disconnects(monkeypatch, capsys):
    import io
    from bot.jsonlog import EventLog
    from bot.service import ChannelError
    from tests.conftest import FakeMeshCore, make_config
    radio = FakeMeshCore()
    async def connected(cfg):
        return radio
    def fail(*args, **kwargs):
        raise ChannelError('Chess requires the optional dependency: pip install -e ".[chess]"')
    monkeypatch.setattr(cli, 'connect', connected)
    monkeypatch.setattr(cli, 'build_service', fail)
    result = await cli.run(make_config(), True, EventLog(stream=io.StringIO()), references=())
    assert result == 3 and radio.disconnected
    error = capsys.readouterr().err
    assert error.startswith('error: Chess requires') and 'Traceback' not in error
