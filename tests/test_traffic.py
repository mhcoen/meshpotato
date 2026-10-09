"""Traffic checks use recorded schema + synthetic timestamps and a fake radio."""
import asyncio
import io
from datetime import datetime

import httpx
import pytest

from bot.config import ConfigError
from bot.jsonlog import EventLog
from bot.lookup_messages import TRAFFIC_UNAVAILABLE
from bot.traffic import CENTRAL, TrafficCache, beltline_direction, cached_traffic_direction, fetch_table, parse_reports
from tests.conftest import FakeBackend, FakeClock, Harness, make_config

NOW = datetime(2026, 10, 9, 8, 0, tzinfo=CENTRAL).timestamp()


def interstate_table(now=NOW):
    return {'recordsFiltered': 2, 'data': [
        {'county': 'Dane', 'routeName': 'I-39/90 NB US 12/18 to Badger Interchange',
         'travelTime': '4', 'travelTimeNominal': '4', 'delay': '0',
         'lastUpdated': datetime.fromtimestamp(now, CENTRAL).strftime('%m/%d/%y, %I:%M %p')},
        {'county': 'Dane', 'routeName': 'I-39/90 SB Badger Interchange to US 12/18',
         'travelTime': '8', 'travelTimeNominal': '3', 'delay': '5',
         'lastUpdated': datetime.fromtimestamp(now, CENTRAL).strftime('%m/%d/%y, %I:%M %p')},
    ]}


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
    assert c.answer('', 130) == 'Beltline: Eastbound 22 min, 5 min delay (2 min ago). Westbound 17 min, no delay (2 min ago). Source: 511.'
    assert 'Eastbound 22 min, 5 min delay' in c.answer('EB', 130)
    assert 'Westbound 17 min, no delay' in c.answer('WB', 130)
    assert c.answer('', 50) == "I can get live traffic, but can't right now."
    clock.advance(479)
    assert '(9 min ago)' in c.answer('', 130)
    clock.advance(1)
    assert '(8:00 AM)' in c.answer('', 130)


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
    assert '5 min ago' in c.answer('', 130)
    clock.advance(300)
    assert '(8:00 AM)' in c.answer('', 130)


async def test_polling_identical_old_source_does_not_reset_age():
    clock = FakeClock(NOW)
    c = cache(clock)
    await c.refresh()
    clock.advance(540)
    assert await c.refresh()
    assert '9 min ago' in c.answer('', 130)
    clock.advance(60)
    assert await c.refresh()
    assert '(8:00 AM)' in c.answer('', 130)


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


def test_future_or_zero_direction_is_not_reported_as_clear():
    for stamp, current in ((NOW + 60, '17'), (NOW, '0')):
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
    assert '(8:00 AM)' in c.answer('', 130)


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
    assert h.sent[-1][1] == '@[Michael] Beltline: Eastbound 22 min, 5 min delay (0 min ago). Westbound 17 min, no delay (0 min ago). Source: 511.'
    assert not h.backend.calls
    assert len(('Mesh Potato: ' + h.sent[-1][1]).encode()) <= 160


async def test_missing_cache_sends_topic_notice_without_model():
    clock = FakeClock(NOW)
    h = Harness(make_config(web_enabled=True), FakeBackend(), clock)
    h.service.traffic = cache(clock)
    await h.say('Michael: Beltline traffic?')
    assert h.sent[-1][1] == '@[Michael] ' + TRAFFIC_UNAVAILABLE
    assert not h.backend.calls


async def test_expiry_during_hold_labels_last_known_measurements():
    clock = FakeClock(NOW)
    h = Harness(make_config(web_enabled=True), FakeBackend(), clock)
    h.service.traffic = cache(clock)
    await h.service.traffic.refresh()
    async def hold(received):
        clock.advance(600)
        return 600000
    h.service._hold_for_quiet_channel = hold
    await h.say('Michael: Beltline traffic?')
    assert '(8:00 AM)' in h.sent[-1][1]
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
    searches = []
    def respond(request):
        assert request.url.host == '511wi.gov'
        query = __import__('json').loads(request.url.params['query'])
        search = query['search']['value']
        searches.append(search)
        assert search in ('Beltline', 'I-39/90')
        assert 'authorization' not in request.headers
        return httpx.Response(200, json=table() if search == 'Beltline' else interstate_table())
    monkeypatch.setattr('bot.traffic.httpx.AsyncClient', lambda **kw: real_client(transport=httpx.MockTransport(respond), **kw))
    result = await fetch_table()
    assert result['recordsFiltered'] == 4
    assert set(searches) == {'Beltline', 'I-39/90'}
    assert set(parse_reports(result, NOW, NOW)) == {'EB', 'WB', 'I90:NB', 'I90:SB'}


async def test_expiry_at_send_updates_both_radio_output_and_record(monkeypatch):
    clock = FakeClock(NOW)
    h = Harness(make_config(web_enabled=True), FakeBackend(), clock)
    h.service.traffic = cache(clock)
    await h.service.traffic.refresh()
    original_send = h.service._send
    async def delayed_send(reply, **kwargs):
        assert 'Eastbound 22 min' in reply
        clock.advance(600)
        return await original_send(reply, **kwargs)
    monkeypatch.setattr(h.service, '_send', delayed_send)
    await h.say('Michael: Beltline traffic?')
    assert '(8:00 AM)' in h.sent[-1][1]
    assert any(r.get('event') == 'traffic_lookup' and r['outcome'] == 'last-known'
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
    assert 'Eastbound 22 min' in h.sent[-1][1]
    assert not h.backend.calls


async def test_historical_source_times_survive_new_fetch_and_fit_actual_sender():
    now = datetime(2026, 10, 9, 4, 24, tzinfo=CENTRAL).timestamp()
    payload = table(now)
    payload['data'][0].update(lastUpdated='10/8/26, 11:06 PM', travelTime='17', delay='0')
    payload['data'][1]['lastUpdated'] = '10/9/26, 4:13 AM'
    clock = FakeClock(now)
    h = Harness(make_config(web_enabled=True, bot_name='Mesh Potato', reply_max_chars=147), FakeBackend(), clock)
    c = h.service.traffic = cache(clock)
    async def fetch():
        return payload
    c.fetch = fetch
    assert await c.refresh()
    await h.say("Michael M7: What's the traffic like on the beltline?")
    reply = h.sent[-1][1]
    assert reply.startswith('@[Michael M7] Beltline: ')
    assert 'Eastbound 17 min, no delay (yesterday 11:06 PM)' in reply
    assert 'Westbound 17 min, no delay (4:13 AM)' in reply
    assert len(('Mesh Potato: '+reply).encode()) <= 160
    assert not h.backend.calls
    assert '4:24' not in reply  # fetching did not refresh the observation


async def test_single_direction_last_update_and_missing_direction():
    now = datetime(2026, 10, 9, 4, 24, tzinfo=CENTRAL).timestamp()
    payload = table(now - 660)
    payload['data'][0]['travelTime'] = '0'
    c = cache(FakeClock(now))
    async def fetch():
        return payload
    c.fetch = fetch
    assert await c.refresh()
    assert c.answer('WB',130) == 'Beltline: Westbound 17 min, no delay (4:13 AM). Source: 511.'
    assert 'Eastbound unavailable.' in c.answer('',130)
    assert c.answer('EB',130) == TRAFFIC_UNAVAILABLE


async def test_last_year_is_explicit_in_historical_timestamp():
    old = datetime(2025, 10, 9, 8, tzinfo=CENTRAL).timestamp()
    c = cache()
    async def fetch():
        return table(old)
    c.fetch = fetch
    assert await c.refresh()
    assert '10/09/2025 8:00 AM' in c.answer('EB',130)


async def test_partial_refresh_uses_latest_each_direction_not_old_combined_snapshot():
    c = cache()
    await c.refresh()
    c.clock.advance(60)
    changed = table(NOW+60)
    changed['data'][0].update(travelTime='27', delay='10')
    changed['data'][1]['travelTime'] = '0'
    async def fetch():
        return changed
    c.fetch = fetch
    assert await c.refresh()
    assert 'Eastbound 27 min, 10 min delay' in c.answer('',130)
    assert 'Westbound 17 min, no delay' in c.answer('',130)
    assert '1 min ago' in c.answer('',130)


@pytest.mark.parametrize('question,direction', [
    ('traffic on i90', 'I90'), ('How is traffic on I-90?', 'I90'),
    ('Madison I 90 northbound traffic?', 'I90:NB'), ('I-39/90 southbound delays?', 'I90:SB'),
    ('Interstate 90 traffic both directions', 'I90'),
    ('I90 traffic in Chicago', None), ('I90 traffic to Janesville', None),
    ('I90 traffic tomorrow', None), ('Why was I90 closed yesterday?', None),
    ('I90 eastbound traffic', None), ('I90 traffic and Packers record', None),
])
def test_i90_queries_are_scoped_to_verified_madison_corridor(question, direction):
    assert cached_traffic_direction(question) == direction
    assert cached_traffic_direction('I90 traffic', 'Chicago, IL') is None


@pytest.mark.parametrize('question', ['traffic on i90', '/traffic I-90', '!traffic I90', '/web I-90 traffic'])
async def test_interstate_questions_use_cached_measurements_and_actual_timestamps(question):
    clock = FakeClock(NOW)
    h = Harness(make_config(web_enabled=True, bot_name='Mesh Potato', reply_max_chars=147), FakeBackend(), clock)
    c = h.service.traffic = cache(clock)
    async def fetch():
        return interstate_table(NOW-3600)
    c.fetch = fetch
    assert await c.refresh()
    async def forbidden(*args):
        pytest.fail('foreground search should not run for a cached corridor')
    h.service.web.search = forbidden
    h.service._backend_retry_at = NOW+100
    await h.say('Michael: '+question)
    answer = h.sent[-1][1]
    assert 'I-90 Beltline to I-94:' in answer
    assert 'Northbound 4 min, no delay (7:00 AM)' in answer
    assert 'Southbound 8 min, 5 min delay (7:00 AM)' in answer
    assert len(('Mesh Potato: '+answer).encode()) <= 160
    assert not h.backend.calls
    assert any(r.get('event') == 'traffic_lookup' and r['outcome'] == 'last-known' for r in h.records)


@pytest.mark.parametrize('question', ['traffic downtown', '/traffic downtown', '!traffic downtown',
                                      '/web downtown traffic', 'How is the traffic?'])
async def test_broad_traffic_gets_useful_clarification_without_search_or_model(question):
    h = Harness(make_config(web_enabled=True), FakeBackend(), FakeClock(NOW))
    async def forbidden(*args):
        pytest.fail('clarification must not call search')
    h.service.web.search = forbidden
    h.service._backend_retry_at = NOW+100
    await h.say('Michael: '+question)
    answer = h.sent[-1][1]
    assert ('which street and direction?' if 'downtown' in question else 'Which road and direction?') in answer
    assert "can't get it right now" not in answer and not h.backend.calls


async def test_downtown_followup_checks_requested_street_not_invented_live_conditions():
    from unittest.mock import AsyncMock
    from bot.lookup_messages import TRAFFIC_UNVERIFIED
    h = Harness(make_config(web_enabled=True), FakeBackend('Traffic is light right now.'), FakeClock(NOW))
    h.service.web.search = AsyncMock(return_value=[])
    await h.say('Michael: traffic downtown')
    h.clock.advance(20)
    await h.say('Michael: John Nolen Drive northbound')
    assert 'traffic on John Nolen Drive northbound' in h.service.web.search.call_args.args[0]
    assert h.sent[-1][1] == '@[Michael] '+TRAFFIC_UNVERIFIED
    assert 'light right now' not in h.sent[-1][1]


@pytest.mark.parametrize('next_sender,elapsed', [('Other', 20), ('Michael', 121)])
async def test_traffic_clarification_is_sender_local_and_expires(next_sender, elapsed):
    from unittest.mock import AsyncMock
    h = Harness(make_config(web_enabled=True), FakeBackend('That street is in Madison.'), FakeClock(NOW))
    h.service.web.search = AsyncMock(return_value=[])
    await h.say('Michael: traffic downtown')
    h.clock.advance(elapsed)
    await h.say(next_sender+': John Nolen Drive northbound')
    assert not h.service.web.search.called


async def test_one_feed_failure_preserves_other_feed_and_old_interstate(monkeypatch):
    import bot.traffic as traffic
    c = cache()
    c.reports = parse_reports(interstate_table(NOW-3600), NOW, NOW)
    old = c.reports.copy()
    async def partial(search):
        if search == 'I-39/90':
            raise httpx.ConnectError('fake outage')
        return table()
    monkeypatch.setattr(traffic, '_fetch_table', partial)
    c.fetch = fetch_table
    assert await c.refresh()
    assert c.reports['I90:NB'] == old['I90:NB']
    assert 'Eastbound 22 min' in c.answer('', 136)
    assert '(7:00 AM)' in c.answer('I90:NB', 136)


async def test_partial_interstate_page_is_not_treated_as_complete(monkeypatch):
    import bot.traffic as traffic
    async def partial(search):
        payload = table() if search == 'Beltline' else interstate_table()
        if search != 'Beltline':
            payload['recordsFiltered'] = 200
        return payload
    monkeypatch.setattr(traffic, '_fetch_table', partial)
    assert set(parse_reports(await fetch_table(), NOW, NOW)) == {'EB', 'WB'}


def test_no_report_for_adjacent_or_alternate_routes_or_missing_measurements():
    p = interstate_table()
    p['data'][0]['routeName'] = 'Sign 339 I-39/90 NB Church St to I-94'
    p['data'][1]['travelTime'] = '0'
    assert parse_reports(p, NOW, NOW) == {}


async def test_forget_during_clarification_does_not_restore_traffic_context(monkeypatch):
    h = Harness(make_config(web_enabled=True, global_burst=3, sender_burst=3), FakeBackend(), FakeClock(NOW))
    entered, release = asyncio.Event(), asyncio.Event()
    async def hold(received):
        entered.set()
        await release.wait()
        return 0
    monkeypatch.setattr(h.service, '_hold_for_quiet_channel', hold)
    question = asyncio.create_task(h.say('Michael: traffic downtown'))
    await asyncio.wait_for(entered.wait(), 1)
    await h.say('Michael: /forget')  # clears data even when its confirmation cannot reserve airtime
    release.set()
    await asyncio.wait_for(question, 1)
    assert not h.service._traffic_clarifications
    assert not h.service.memory.rounds_for('Michael')
