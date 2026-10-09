"""Daily weather presentation and evidence checks, with no network or radio."""
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit
from unittest.mock import AsyncMock

import pytest

from bot.config import WIRE_TEXT_MAX
from bot.service import Decision
from bot.weather import weather_location, collect_weather, weather_answer, UNAVAILABLE, CLARIFY
from bot.web import search_query
from tests.conftest import FakeBackend

NOW = datetime(2026, 10, 9, 15, tzinfo=timezone.utc)


def forecast(now=NOW):
    local = now.astimezone(timezone(timedelta(hours=-5)))
    return {'utc_offset_seconds': -18000,
            'current_units': {'temperature_2m': '°F', 'wind_speed_10m': 'mp/h',
                              'wind_direction_10m': '°', 'weather_code': 'wmo code'},
            'daily_units': {'temperature_2m_max': '°F', 'temperature_2m_min': '°F', 'weather_code': 'wmo code'},
            'current': {'time': local.replace(tzinfo=None).isoformat(timespec='minutes'),
                        'temperature_2m': 55, 'weather_code': 0,
                        'wind_speed_10m': 5, 'wind_direction_10m': 292.5},
            'daily': {'time': [local.date().isoformat(), (local.date()+timedelta(days=1)).isoformat()],
                      'weather_code': [0, 3], 'temperature_2m_max': [73, 65], 'temperature_2m_min': [55, 48]}}


def fetcher(now=NOW, city='Madison', region='Wisconsin'):
    def fetch(url):
        if urlsplit(url).hostname == 'geocoding-api.open-meteo.com':
            assert parse_qs(urlsplit(url).query)['name'] == [city+', '+region]
            return {'results': [{'name': city, 'admin1': region, 'country': 'United States',
                                 'country_code': 'US', 'latitude': 43.1, 'longitude': -89.4}]}
        assert urlsplit(url).hostname == 'api.open-meteo.com'
        query = parse_qs(urlsplit(url).query)
        assert query['temperature_unit'] == ['fahrenheit'] and query['wind_speed_unit'] == ['mph']
        assert query['forecast_days'] == ['2']
        return forecast(now)
    return fetch


def pages(now=NOW):
    return collect_weather('weather Madison, Wisconsin', fetcher(now), now)


@pytest.mark.parametrize('prompt,place', [
    ('wx', 'Madison, WI'), ('Weather', 'Madison, WI'),
    ('what is the weather?', 'Madison, WI'),
    ('What is the weather in Chicago, IL?', 'Chicago, IL'),
    ('weather Paris, France tomorrow', 'Paris, France'),
    ('daily weather for Seattle, WA', 'Seattle, WA'),
    ('forecast for London, UK', 'London, UK'),
])
def test_location_parsing_keeps_explicit_location(prompt, place):
    assert weather_location(prompt, 'Madison, WI') == place
    assert search_query(prompt, 'Madison, WI', NOW) == f'weather {place} (as of 2026-10-09)'


@pytest.mark.parametrize('prompt', ['weather last week', 'weather in Madison yesterday',
                                   'weather in Madison next week', 'How does weather work?',
                                   'Is it raining?', 'Why is the sky blue?'])
def test_other_weather_questions_keep_general_web_path(prompt):
    assert weather_location(prompt) is None


def test_exact_requested_format_from_fixture_not_real_weather():
    answer, evidence = weather_answer(pages(), NOW, 200)
    assert answer == '🌤️ Daily Weather: Madison, WI: ☀️Clear 55°F WNW5mph | H:73°F L:55°F | Tomorrow: ☁️Overcast H:65°F L:48°F'
    assert evidence['location'] == 'Madison, WI'


def test_compact_format_keeps_every_weather_field_within_bytes():
    answer, evidence = weather_answer(pages(), NOW, 115)
    assert evidence and len(answer.encode()) <= 115
    for field in ['Madison, WI', 'Clear', '55', 'WNW5mph', 'H:73', 'L:55', 'Overcast', 'H:65', 'L:48']:
        assert field in answer


@pytest.mark.parametrize('change', [
    lambda p: p.update(fetched_at=(NOW-timedelta(hours=1)).isoformat()),
    lambda p: p.update(fetched_at=(NOW+timedelta(hours=1)).isoformat()),
    lambda p: p['weather']['current'].update(time='2026-10-08T10:00'),
    lambda p: p['weather']['daily'].update(time=['2026-10-08','2026-10-09']),
    lambda p: p['weather']['current_units'].update(temperature_2m='°C'),
    lambda p: p['weather']['current'].update(temperature_2m=None),
    lambda p: p['weather']['current'].update(temperature_2m=float('nan')),
    lambda p: p['weather']['daily'].update(temperature_2m_max=[73]),
    lambda p: p['weather']['daily'].update(temperature_2m_min=[90, 48]),
    lambda p: p['weather']['current'].update(weather_code=4),
    lambda p: p.update(location='@[someone]'),
    lambda p: p.update(url='https://example.com/forecast'),
])
def test_bad_or_stale_evidence_never_produces_weather(change):
    data = pages(); change(data[0])
    assert weather_answer(data, NOW, 150) == (UNAVAILABLE, None)


def test_ambiguous_or_wrong_state_is_not_silently_selected():
    rows = [{'name':'Madison','admin1':s,'country_code':'US','latitude':43,'longitude':-89}
            for s in ('Wisconsin','Alabama')]
    result = collect_weather('weather Madison', lambda _: {'results': rows}, NOW)
    assert weather_answer(result, NOW, 150) == (CLARIFY, None)
    result = collect_weather('weather Madison, California', lambda _: {'results': rows}, NOW)
    assert weather_answer(result, NOW, 150) == (CLARIFY, None)


@pytest.mark.parametrize('prompt', ['wx', 'what is the weather?'])
async def test_weather_service_uses_typed_data_and_preserves_unicode(harness, prompt):
    h = harness(bot_name='Mesh Potato', reply_max_chars=147, web_enabled=True,
                backend=FakeBackend(error=AssertionError('weather must not call model')))
    # The worker's fetch timestamp is later than the service's lookup start.
    h.service.web.search = AsyncMock(side_effect=lambda _: pages(datetime.now(timezone.utc)))
    assert await h.say('Michael: '+prompt) == Decision.ANSWERED
    reply = h.sent[0][1]
    assert 'Madison, WI' in reply and '°F' in reply and 'Overcast' in reply
    assert len(('Mesh Potato: '+reply).encode()) <= WIRE_TEXT_MAX
    assert any(r['event']=='weather_lookup' and r['outcome']=='answer' for r in h.records)
    assert not h.backend.calls


async def test_missing_weather_data_has_notice_without_model(harness):
    h = harness(web_enabled=True, backend=FakeBackend(error=AssertionError('no model')))
    h.service.web.search = AsyncMock(return_value=[])
    assert await h.say('Agent Winter: wx') == Decision.ANSWERED
    assert UNAVAILABLE in h.sent[-1][1] and not h.backend.calls


async def test_stale_weather_is_not_sent_after_pause(harness, clock):
    h = harness(web_enabled=True)
    h.service.web.search = AsyncMock(return_value=pages(datetime.now(timezone.utc)))
    async def hold(*args):
        clock.advance(901)
        return 901000
    h.service._hold_for_quiet_channel = hold
    assert await h.say('Michael: Weather') == Decision.DROP_QUEUE_EXPIRED
    assert not h.sent and h.inbound_records()[-1]['reason']=='stale-weather'


def test_worker_routes_weather_without_general_search(monkeypatch):
    from bot import web_sources
    monkeypatch.setattr(web_sources, 'fetch_page', lambda url, **kw: fetcher()(url))
    monkeypatch.setattr(web_sources, 'DDGS', lambda **kw: pytest.fail('must not search'))
    assert web_sources.collect('weather Madison, Wisconsin')[0]['location']=='Madison, WI'
