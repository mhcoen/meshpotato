"""Traffic checks use recorded schema + synthetic timestamps and a fake radio."""
import asyncio
import io
from datetime import datetime

import httpx
import pytest

from bot.config import ConfigError
from bot.jsonlog import EventLog
from bot.lookup_messages import TRAFFIC_UNAVAILABLE
from bot.traffic import CENTRAL, TrafficCache, beltline_direction, fetch_table, parse_reports
from tests.conftest import FakeBackend, FakeClock, Harness, make_config

NOW = datetime(2026, 10, 9, 8, 0, tzinfo=CENTRAL).timestamp()


def table(now=NOW):
    return {'recordsFiltered': 2, 'data': [
        {'county': 'Dane', 'routeName': 'US 12 EB University Ave to Beltline Interchange',
         'travelTime': '22', 'travelTimeNominal': '17', 'delay': '5',
         'lastUpdated': datetime.fromtimestamp(now, CENTRAL).strftime('%m/%d/%y, %I:%M %p')},
        {'county': 'Dane', 'routeName': 'US 12 WB Beltline Interchange to University Ave',
         'travelTime': '17', 'travelTimeNominal': '17', 'delay': '0',
         'lastUpdated': datetime.fromtimestamp(now, CENTRAL).strftime('%m/%d/%y, %I:%M %p')},
    ]}


def cache(clock=None):
    clock = clock or FakeClock(NOW)
    async def fetch():
        return table()
    return TrafficCache(EventLog(stream=io.StringIO()), fetch=fetch, clock=clock, wall_clock=clock)


@pytest.mark.parametrize('prompt,direction', [
    ('What is the traffic on the Beltline?', ''),
    ("What's the traffic on the belt line right now?", ''),
    ('Any delays on the Beltline eastbound?', 'EB'),
    ('Beltline WB travel time?', 'WB'),
    ('How is traffic on the Madison Beltline?', ''),
    ('Beltline traffic both directions?', ''),
    ('Beltline eastbound and westbound traffic?', ''),
])
def test_routing(prompt, direction):
    assert beltline_direction(prompt) == direction


@pytest.mark.parametrize('prompt', [
    'What is the weather on the Beltline?', 'Beltline traffic in Atlanta?',
    'Beltline traffic tomorrow?', 'Why is Beltline traffic slow?',
    'Beltline traffic from Verona Road to Park Street?', 'Beltline accidents?',
    'Traffic on the Beltline and I-90?', 'What is a beltline?',
    'Beltline closures?', 'Beltline traffic last night?',
])
def test_does_not_answer_other_questions_with_corridor_times(prompt):
    assert beltline_direction(prompt) is None


def test_location_scope():
    assert beltline_direction('Beltline traffic?', 'Atlanta, Georgia') is None
    assert beltline_direction('Madison Beltline traffic?', 'Atlanta, Georgia') == ''


async def test_ready_report_and_original_source_age():
    clock = FakeClock(NOW + 120)
    c = cache(clock)
    assert await c.refresh()
    assert c.answer('', 130) == 'Beltline Univ<>I-39/90: EB 22m (+5); WB 17m (+0). 511 2m ago; +delay.'
    assert 'Univ to I-39/90: EB 22m' in c.answer('EB', 130)
    assert 'I-39/90 to Univ: WB 17m' in c.answer('WB', 130)
    assert c.answer('', 50) == "I can get live traffic, but can't right now."
    clock.advance(479)
    assert '+delay' in c.answer('', 130)
    clock.advance(1)
    assert c.answer('', 130) == TRAFFIC_UNAVAILABLE


async def test_failed_refresh_preserves_last_good_and_never_rejuvenates_it():
    clock = FakeClock(NOW)
    c = cache(clock)
    await c.refresh()
    old = c.reports.copy()
    async def broken():
        raise RuntimeError('failed')
    c.fetch = broken
    clock.advance(300)
    assert not await c.refresh()
    assert c.reports == old
    assert '5m ago' in c.answer('', 130)
    clock.advance(300)
    assert c.answer('', 130) == TRAFFIC_UNAVAILABLE


async def test_polling_identical_old_source_does_not_reset_age():
    clock = FakeClock(NOW)
    c = cache(clock)
    await c.refresh()
    clock.advance(540)
    assert await c.refresh()
    assert '9m ago' in c.answer('', 130)
    clock.advance(60)
    assert not await c.refresh()
    assert c.answer('', 130) == TRAFFIC_UNAVAILABLE


@pytest.mark.parametrize('field,value', [('travelTime', 'NaN'), ('delay', '-1'),
                                       ('travelTimeNominal', None), ('lastUpdated', 'bad'),
                                       ('delay', '15')])
def test_rejects_invalid_measurements(field, value):
    payload = table()
    payload['data'][0][field] = value
    with pytest.raises(ValueError):
        parse_reports(payload, NOW, 1000)


@pytest.mark.parametrize('mutate', [
    lambda p: p.update(recordsFiltered=3),
    lambda p: p.update(data=[]),
    lambda p: p.update(data='bad'),
])
def test_no_false_clear_roads_from_incomplete_feed(mutate):
    payload = table()
    mutate(payload)
    with pytest.raises(ValueError):
        parse_reports(payload, NOW, 1000)


def test_missing_stale_future_or_zero_direction_is_not_reported_as_clear():
    for stamp, current in ((NOW - 600, '17'), (NOW + 60, '17'), (NOW, '0')):
        payload = table()
        payload['data'][1]['lastUpdated'] = datetime.fromtimestamp(stamp, CENTRAL).strftime('%m/%d/%y, %I:%M %p')
        payload['data'][1]['travelTime'] = current
        result = parse_reports(payload, NOW, 1000)
        assert set(result) == {'EB'}
    payload = table()
    for row in payload['data']:
        row['county'] = 'Milwaukee'
    assert parse_reports(payload, NOW, 1000) == {}


async def test_clock_rollback_and_fetch_age_fail_closed():
    c = cache()
    await c.refresh()
    c.wall_clock = lambda: NOW - 1
    assert c.answer('', 130) == TRAFFIC_UNAVAILABLE
    c.wall_clock = lambda: NOW
    c.clock = lambda: NOW + 600
    assert c.answer('', 130) == TRAFFIC_UNAVAILABLE


async def test_background_worker_starts_once_and_cancels_inflight_fetch():
    c = cache()
    entered, cancelled = asyncio.Event(), asyncio.Event()
    async def blocking():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    c.fetch = blocking
    c.start()
    first = c._task
    c.start()
    assert c._task is first
    await asyncio.wait_for(entered.wait(), 1)
    await c.stop()
    assert cancelled.is_set() and first.done() and c._task is None


@pytest.mark.parametrize('question', ['What is the traffic on the Beltline?', '/web Beltline traffic?'])
async def test_query_bypasses_model_and_web_including_model_cooldown(question):
    clock = FakeClock(NOW)
    h = Harness(make_config(web_enabled=True, bot_name='Mesh Potato', reply_max_chars=147), FakeBackend(), clock)
    h.service.traffic = cache(clock)
    await h.service.traffic.refresh()
    async def forbidden(*args):
        pytest.fail('foreground web search called')
    h.service.web.search = forbidden
    h.service._backend_retry_at = NOW + 1000
    await h.say('Michael: ' + question)
    assert h.sent[-1][1] == '@[Michael] Beltline Univ<>I-39/90: EB 22m (+5); WB 17m (+0). 511 0m ago; +delay.'
    assert not h.backend.calls
    assert len(('Mesh Potato: ' + h.sent[-1][1]).encode()) <= 160


async def test_missing_cache_sends_topic_notice_without_model():
    clock = FakeClock(NOW)
    h = Harness(make_config(web_enabled=True), FakeBackend(), clock)
    h.service.traffic = cache(clock)
    await h.say('Michael: Beltline traffic?')
    assert h.sent[-1][1] == '@[Michael] ' + TRAFFIC_UNAVAILABLE
    assert not h.backend.calls


async def test_expiry_during_hold_records_actual_notice_instead_of_stale_report():
    clock = FakeClock(NOW)
    h = Harness(make_config(web_enabled=True), FakeBackend(), clock)
    h.service.traffic = cache(clock)
    await h.service.traffic.refresh()
    async def hold(received):
        clock.advance(600)
        return 600000
    h.service._hold_for_quiet_channel = hold
    await h.say('Michael: Beltline traffic?')
    assert h.sent[-1][1] == '@[Michael] ' + TRAFFIC_UNAVAILABLE
    assert any(r.get('reply') == h.sent[-1][1] for r in h.records)
    assert not h.backend.calls


async def test_service_owns_worker_lifecycle_without_auto_posting_traffic():
    h = Harness(make_config(web_enabled=True, fortune_enabled=False), FakeBackend(), FakeClock(NOW))
    c = h.service.traffic = cache(h.clock)
    await h.service.start()
    task = c._task
    assert task is not None
    await asyncio.sleep(0)
    await h.service.stop()
    assert task.done() and c._task is None
    assert not any('Beltline' in reply for _, reply in h.sent)


@pytest.mark.parametrize('interval', [0, 59, 601, float('inf')])
def test_refresh_config_limits(interval):
    with pytest.raises(ConfigError):
        make_config(traffic_refresh_s=interval)


async def test_transport_is_bounded_fixed_origin_and_no_credentials(monkeypatch):
    real_client = httpx.AsyncClient
    def respond(request):
        assert request.url.host == '511wi.gov'
        query = __import__('json').loads(request.url.params['query'])
        assert query['search']['value'] == 'Beltline'
        assert 'authorization' not in request.headers
        return httpx.Response(200, json=table())
    monkeypatch.setattr('bot.traffic.httpx.AsyncClient', lambda **kw: real_client(transport=httpx.MockTransport(respond), **kw))
    assert await fetch_table() == table()


async def test_expiry_at_send_updates_both_radio_output_and_record(monkeypatch):
    clock = FakeClock(NOW)
    h = Harness(make_config(web_enabled=True), FakeBackend(), clock)
    h.service.traffic = cache(clock)
    await h.service.traffic.refresh()
    original_send = h.service._send
    async def delayed_send(reply, **kwargs):
        assert 'EB 22m' in reply
        clock.advance(600)
        return await original_send(reply, **kwargs)
    monkeypatch.setattr(h.service, '_send', delayed_send)
    await h.say('Michael: Beltline traffic?')
    assert h.sent[-1][1] == '@[Michael] ' + TRAFFIC_UNAVAILABLE
    assert any(r.get('event') == 'traffic_lookup' and r['outcome'] == 'unavailable'
               and r['reply'] == h.sent[-1][1] for r in h.records)
    assert not h.backend.calls


async def test_long_name_has_fitting_unavailable_notice():
    clock = FakeClock(NOW)
    h = Harness(make_config(web_enabled=True), FakeBackend(), clock)
    h.service.traffic = cache(clock)
    await h.say('A' * 60 + ': Beltline traffic?')
    assert h.sent and 'live traffic' in h.sent[-1][1]
    assert len(h.sent[-1][1]) <= 150


@pytest.mark.parametrize('enabled,web', [(True, True), (False, True), (True, False)])
async def test_cli_wires_cache_only_when_enabled(enabled, web):
    from bot.cli import build_service
    from tests.conftest import FakeMeshCore
    cfg = make_config(traffic_enabled=enabled, web_enabled=web, fortune_enabled=False, adaptive_enabled=False)
    service = build_service(cfg, FakeMeshCore(), EventLog(stream=io.StringIO()), references=())
    assert (service.traffic is not None) == (enabled and web)
    if service.traffic is not None:
        assert service.traffic.refresh_s == 300
        assert service.traffic._task is None
    await service.stop()


@pytest.mark.parametrize('prompt', [
    'can you give me a traffic update on the Madison Beltline (Hwy12/18)',
    'Hey MeshPotato, can you give me a traffic update on the Madison Beltline (Hwy12/18)',
    'Could you give a Beltline traffic report please?',
])
async def test_historical_beltline_wording_uses_cache(prompt):
    clock = FakeClock(NOW)
    h = Harness(make_config(web_enabled=True), FakeBackend(), clock)
    h.service.traffic = cache(clock)
    await h.service.traffic.refresh()
    async def forbidden(*args):
        pytest.fail('historical Beltline wording missed cache')
    h.service.web.search = forbidden
    await h.say('Michael: /web '+prompt)
    assert 'EB 22m' in h.sent[-1][1]
    assert not h.backend.calls
