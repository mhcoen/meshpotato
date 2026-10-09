"""Headless terminal layout and routing tests, with no radio/model/network access."""
import pytest
from textual.widgets import RichLog, Tabs

from bot.service import Stats
from bot.tui import MeshPotatoApp
from tests.conftest import Harness, FakeBackend, FakeClock, make_config


def monitor(slots=(1, 2, 3)):
    cfg = make_config(chess_channel_idx=3 if 3 in slots else -1,
                      traffic_channel_idx=2 if 2 in slots else -1,
                      state_db='/tmp/unused-tui-test.sqlite3', announce_startup=False)
    h = Harness(cfg, FakeBackend(), FakeClock())
    names = {1: '#ai', 2: '#traffic', 3: '#chess', 4: '#garden'}
    stats = {idx: Stats(channel_idx=idx, channel_name=names[idx]) for idx in slots}
    async def nothing():
        pass
    app = MeshPotatoApp(cfg, stats[1], h.limiter, lambda listener: None, nothing, nothing,
                        channel_stats=stats)
    return app


def send(app, idx, event='inbound', **fields):
    app._on_record(dict(ts='2026-10-09T15:00:00Z', channel_idx=idx, event=event,
                        sender='Alice', prompt='[bold]literal[/bold]', decision='answered',
                        reply='Reply '+str(idx), **fields))


def displayed(app, selector):
    return '\n'.join(line.text for line in app.query_one(selector, RichLog).lines)


async def test_columns_route_messages_alerts_and_shared_errors():
    app = monitor()
    async with app.run_test(size=(160, 44)) as pilot:
        await pilot.pause()
        assert not app.query_one('#channel-tabs').display
        panels = [app.query_one(f'#channel-{idx}') for idx in (1, 2, 3)]
        assert all(panel.display for panel in panels)
        assert panels[0].region.right <= panels[1].region.x
        assert panels[1].region.right <= panels[2].region.x
        for idx in (1, 2, 3):
            send(app, idx)
        send(app, 2, 'traffic_alert_post', outcome='sent', text='WIS 19 closed tomorrow.')
        send(app, 3, 'send_error', error='Radio unavailable')
        await pilot.pause()
        for idx in (1, 2, 3):
            text = displayed(app, f'#channel-{idx}')
            assert f'Reply {idx}' in text
            assert '[bold]literal[/bold]' in text
            assert all(f'Reply {other}' not in text for other in (1, 2, 3) if other != idx)
        assert 'WIS 19' in displayed(app, '#channel-2')
        assert 'Radio unavailable' in displayed(app, '#log')
        assert 'Reply 1' not in displayed(app, '#log')


async def test_resize_tabs_preserve_hidden_messages_and_selection():
    app = monitor()
    async with app.run_test(size=(160, 44)) as pilot:
        await pilot.pause()
        send(app, 1)
        await pilot.resize_terminal(80, 24)
        await pilot.pause()
        assert app.query_one('#channel-tabs').display
        assert app.query_one('#compact-status').display
        assert app.query_one('#channel-1').display
        assert not app.query_one('#channel-2').display
        send(app, 2)
        await pilot.press('n')
        await pilot.pause()
        assert app._active_slot == 2 and app.query_one('#channel-2').display
        assert 'Reply 2' in displayed(app, '#channel-2')
        app.query_one('#channel-tabs', Tabs).active = 'tab-3'
        await pilot.pause()
        assert app._stats.channel_idx == 3
        await pilot.resize_terminal(170, 44)
        await pilot.pause()
        assert all(app.query_one(f'#channel-{idx}').display for idx in (1,2,3))
        assert app._active_slot == 3
        assert 'Reply 1' in displayed(app, '#channel-1')
        assert 'Reply 2' in displayed(app, '#channel-2')


async def test_fourth_channel_automatically_gets_panel_and_tab():
    app = monitor((1, 2, 3, 4))
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        assert app.query_one('#tab-4').label.plain == '#garden'
        send(app, 4)
        app.query_one('#channel-tabs', Tabs).active = 'tab-4'
        await pilot.pause()
        assert app.query_one('#channel-4').display
        assert 'Reply 4' in displayed(app, '#channel-4')
        await pilot.resize_terminal(200, 44)
        await pilot.pause()
        assert all(app.query_one(f'#channel-{idx}').display for idx in (1,2,3,4))


async def test_single_channel_unscoped_events_and_bounded_history():
    app = monitor((1,))
    async with app.run_test(size=(80,24)) as pilot:
        await pilot.pause()
        send(app, None)
        await pilot.pause()
        assert 'Reply None' in displayed(app, '#channel-1')
        assert not app.query_one('#channel-tabs').display
        for i in range(2100):
            send(app, 1, 'announce', text=str(i))
        assert len(app._lines[1]) == 2000
