"""Regressions from the October channel audit; all feeds and radios are fake."""
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from meshcore import EventType

from bot import sports
from bot.quality import unsupported_action
from bot.reception import asks_about_reception
from bot.service import Decision
from bot.sports_queries import sports_kind, with_team_context
from bot.sports_series import collect_series
from bot.self_knowledge import factual_self_reply
from bot.traffic.alerts import FINGERPRINT_VERSION
from bot.traffic.alerts import select_alerts
from bot.traffic.channel import TrafficChannel
from bot.web import needs_web
from tests.conftest import FakeBackend, make_config
from tests.test_sports import NOW, page, service_clock
from tests.test_sports_details import scheduled, standing, next_page
from tests.test_traffic_channel import NOW as TRAFFIC_NOW, FakeAPI, incident, snapshot, traffic


@pytest.mark.parametrize('query,kind', [
    ("What time is tonight's nlcs game?", 'game'),
    ('Time tonight’s brewers game ?', 'game'),
    ('What time is brewers game', 'game'),
    ('ALCS game time tonight?', 'game'),
    ('When is the World Series?', 'series_next'),
    ('When is the National League Championship Series game?', 'series_next'),
    ('Next game?', 'next'),
])
def test_historical_schedule_wording_uses_verified_feeds(query, kind):
    assert sports_kind(query) == kind
    assert needs_web(query)


@pytest.mark.parametrize('term,meaning', [
    ('ALCS', 'American League Championship Series'),
    ('NLCS', 'National League Championship Series'),
    ('LCS', 'ALCS for the American League, NLCS for the National League'),
    ('World Series', 'American and National League champions'),
])
async def test_series_definitions_work_without_model_or_lookup(harness, term, meaning):
    h = harness(backend=FakeBackend(error=RuntimeError('offline')))
    assert await h.say(f'Michael: What is {term}?') is Decision.ANSWERED
    assert meaning in h.sent[-1][1]
    assert not h.backend.calls


def postseason(label='NLCS', offset=0, state='pre', ident='401872927'):
    e = scheduled(state, 'STATUS_SCHEDULED' if state == 'pre' else 'STATUS_FINAL', offset)
    e['id'] = ident
    e['competitions'][0]['notes'] = [{'type': 'event', 'headline': label+' - Game 3'}]
    return e


@pytest.mark.parametrize('label', ['ALCS', 'NLCS', 'World Series'])
async def test_series_game_time_is_typed_and_never_invented(harness, service_clock, label):
    h = harness(web_enabled=True, backend=FakeBackend('Game starts at 7:05 PM.'))
    h.service.web.search = AsyncMock(return_value=[page(postseason(label), 'mlb')])
    await h.say(f'Michael M7: What time is tonight’s {label} game?')
    assert 'scheduled 15:25 CDT' in h.sent[-1][1]
    assert '09/13' in h.sent[-1][1] and not h.backend.calls
    assert len((h.cfg.bot_name+': '+h.sent[-1][1]).encode()) <= 160


@pytest.mark.parametrize('mutate', [
    lambda e: e['competitions'][0].update(notes=[]),
    lambda e: e['competitions'][0].update(notes=[{'type': 'event', 'headline': 'ALCS - Game 3'}]),
    lambda e: e['competitions'][0].update(timeValid=False),
    lambda e: e['competitions'][0].update(date='2026-09-15T20:25Z'),
])
def test_series_requires_correct_label_date_and_confirmed_time(mutate):
    e = postseason()
    mutate(e)
    assert sports.sports_answer([page(e, 'mlb')], 'NLCS game time tonight?', NOW, 125) == (sports.UNAVAILABLE, None)


def test_next_series_scan_ignores_other_competitions_and_uses_earliest():
    calls = []
    def fetch(url):
        calls.append(url)
        assert '/baseball/mlb/scoreboard?' in url
        events = [postseason('ALCS', 1, ident='111'), postseason('NLCS', 2), postseason('NLCS', 3, ident='333')]
        return {'leagues': [{'slug': 'mlb'}], 'events': [e for e in events
                if e['competitions'][0]['date'][:10].replace('-', '') in url]}
    query = 'When is the NLCS game?'
    pages = collect_series(query, fetch, NOW)
    answer, evidence = sports.sports_answer(pages, query, NOW, 125)
    assert answer.startswith('NLCS: Brewers vs Reds; scheduled 15:25 CDT, 09/15')
    assert evidence and len(calls) == 6
    assert sports.sports_answer(pages, query, NOW+timedelta(seconds=61), 125) == (sports.UNAVAILABLE, None)


def test_series_schedule_fails_closed_on_feed_failure_or_ambiguity():
    def bad(url):
        return {'events': []}
    query = 'When is the World Series?'
    pages = collect_series(query, bad, NOW)
    assert sports.sports_answer(pages, query, NOW, 125) == (sports.UNAVAILABLE, None)
    pages = [page(postseason('World Series', 1, ident=i), 'mlb') for i in ('111', '222')]
    assert sports.sports_answer(pages, query, NOW, 125) == (sports.CLARIFY, None)
    assert 'ALCS or NLCS' in sports.sports_answer([], 'When is LCS?', NOW, 125)[0]


async def test_bare_followups_keep_sender_team_and_refresh(harness, service_clock):
    h = harness(web_enabled=True, global_burst=6, sender_burst=6)
    h.service.web.search = AsyncMock(side_effect=[
        [standing()], [next_page()], [page(scheduled('post', 'STATUS_FINAL', 0), 'mlb')]])
    for question in ('Brewers standings?', 'Next game?', "what's the score?"):
        await h.say('Michael: '+question)
    for call in h.service.web.search.call_args_list[1:]:
        assert 'Milwaukee Brewers, MLB' in call.args[0]
    assert 'Next: Brewers at Reds' in h.sent[1][1]
    assert 'final, 09/13' in h.sent[2][1]
    await h.say('Alice: Next game?')
    assert h.sent[-1][1] == '@[Alice] Which team do you mean?'
    assert h.service.web.search.await_count == 3 and not h.backend.calls


def test_opponent_correction_keeps_original_team_but_not_unrelated_chat():
    context = {'team': 'Buffalo Bills', 'league': 'nfl'}
    assert 'Buffalo Bills, NFL' in with_team_context('No, their game yesterday against the Rams', context)
    for query in ("how's the sun doing today, brutal heat", 'When is the World Series?', 'Packers score?'):
        assert with_team_context(query, context) == query


def test_recent_result_can_use_yesterday_but_not_future_or_old_game():
    query = 'Did the Brewers win?'
    yesterday = scheduled('post', 'STATUS_FINAL', -1)
    tomorrow = scheduled(offset=1)
    answer, evidence = sports.sports_answer([page(yesterday, 'mlb'), page(tomorrow, 'mlb')], query, NOW, 125)
    assert evidence and 'final, 09/12' in answer
    assert sports.sports_answer([page(scheduled('post', 'STATUS_FINAL', -2), 'mlb')], query, NOW, 125) == (sports.UNAVAILABLE, None)


@pytest.mark.parametrize('query', [
    'What is the timeout on a packet?', 'How many hops with 3 byte IDs?',
    'Do packets expire after 20 hops?', 'Explain hop limits',
    "I was not asking about my signal; how does forwarding work?",
])
def test_protocol_questions_are_not_personal_signal_reports(query):
    assert not asks_about_reception(query)


@pytest.mark.parametrize('query', ['How is my signal?', 'How many hops did my message take?', 'Can you hear me?'])
def test_personal_signal_questions_still_work(query):
    assert asks_about_reception(query)


async def test_tip_removal_does_not_claim_operator_action(harness):
    h = harness()
    await h.say('Michael: That is a ridiculous tip. Make a note to remove it.')
    assert 'operator' in h.sent[-1][1] and 'cannot change' in h.sent[-1][1]
    assert not h.backend.calls


@pytest.mark.parametrize('reply', [
    "I've noted your feedback and will remove the tip.",
    'I will change my settings.', 'I have upgraded the model.',
])
def test_unsupported_actions_are_rejected(reply):
    assert unsupported_action(reply)


def test_ordinary_advice_is_not_mistaken_for_tip_configuration():
    assert factual_self_reply('Any tips to remove rust?', make_config(), 'nice') == ''


async def test_unsupported_action_is_retried_before_transmission(harness):
    h = harness(backend=FakeBackend(replies=[
        "I've noted your feedback and will remove the tip.",
        'My operator can change the scheduled tips.']))
    await h.say('Michael: What can you do about that?')
    assert len(h.backend.calls) == 2
    assert h.sent[-1][1] == '@[Michael] My operator can change the scheduled tips.'


async def test_address_lookup_failure_cannot_invent_address(harness):
    h = harness(web_enabled=True, backend=FakeBackend('123 Imaginary Street.'))
    h.service.web.search = AsyncMock(return_value=[])
    await h.say("Michael: What's the address of the Menard's in Monona?")
    assert h.service.web.search.await_count == 1
    assert 'verify' in h.sent[-1][1] and 'Imaginary Street' not in h.sent[-1][1]


@pytest.mark.parametrize('query', ['What is the address of Menards?', 'Where is the Menards store?'])
def test_store_addresses_require_lookup(query):
    assert needs_web(query)


def silver(message=None):
    return dict(Id=1749, Message=message or (
        'Silver Alert: missing person Jane Example, age 78, last seen in Dane County, '
        'driving a silver sedan. Contact local police with any information. '*4),
        LastUpdated=TRAFFIC_NOW, StartTime=TRAFFIC_NOW-60,
        EndTime=TRAFFIC_NOW+3600, HighImportance=True, Regions=['Dane'])


async def test_long_silver_alert_transmits_subject_and_details(traffic):
    h, worker, _ = traffic
    worker.api.data = snapshot([], alerts=[silver()])
    assert await worker.refresh()
    await worker.publish_pending()
    text = h.sent[-1][1]
    assert 'Silver Alert: missing person Jane Example, age 78' in text
    assert 'Details: 511wi.gov' in text and 'high-importance alert' not in text
    assert len((h.cfg.bot_name+': '+text).encode()) <= 160 and not h.backend.calls


def test_alert_content_changes_are_material_and_html_is_cleaned():
    row = silver('<p>Silver Alert: missing person Jane Example &amp; her dog.</p>')
    first = select_alerts(snapshot([], alerts=[row]), TRAFFIC_NOW, {'dane'})[0]
    second = select_alerts(snapshot([], alerts=[dict(row, Message=row['Message'].replace('Jane', 'John'))]), TRAFFIC_NOW, {'dane'})[0]
    assert first.fingerprint != second.fingerprint
    assert first.render(TRAFFIC_NOW, 147) == 'Silver Alert: missing person Jane Example & her dog.'


@pytest.mark.parametrize('failed_send', [False, True])
async def test_duplicate_source_ids_do_not_retransmit_after_restart(traffic, failed_send):
    h, worker, _ = traffic
    row = dict(incident(), Description='Road closed on US 51 in both directions near Main St.', RoadwayName='US-51')
    duplicate = dict(row, ID=23, DirectionOfTravel='W')
    worker.api.data = snapshot([row, duplicate])
    assert await worker.refresh() and len(worker.current) == 1
    if failed_send:
        h.mc.commands.send_result_type = EventType.ERROR
    await worker.publish_pending()
    restored = TrafficChannel(h.service, FakeAPI(snapshot([duplicate])), clock=worker.clock, wall_clock=worker.wall_clock)
    restored.prepare(h.service._state_store)
    assert await restored.refresh()
    await restored.publish_pending()
    assert len(h.sent) == 1


def test_distinct_directions_are_not_merged():
    row = incident()
    opposite = dict(row, ID=23, DirectionOfTravel='W', Description=row['Description'].replace('eastbound', 'westbound'))
    assert len(select_alerts(snapshot([row, opposite]), TRAFFIC_NOW, {'dane'})) == 2


@pytest.mark.parametrize('attempted', [False, True])
async def test_version_two_receipts_preserve_claims_during_upgrade(traffic, attempted):
    h, worker, _ = traffic
    row = incident()
    old = {'version': 1, 'initialized': True, 'seen': {'event:22': {
        'fingerprint': 'a'*64, 'fingerprint_version': 2, 'at': TRAFFIC_NOW-100,
        'full': True, 'blocked_lanes': None, 'hazards': ['multiple vehicles'], 'attempted': attempted}}}
    h.service._state_store.save_traffic(old)
    worker.prepare(h.service._state_store)
    worker.api.data = snapshot([row, dict(row, ID=23)])
    assert await worker.refresh()
    receipt = worker.state['seen']['event:22']
    assert receipt['fingerprint_version'] == FINGERPRINT_VERSION
    assert receipt['at'] == TRAFFIC_NOW-100 and receipt['attempted'] is attempted
    await worker.publish_pending()
    assert not h.sent


def test_silver_alert_after_boilerplate_retains_missing_person_details():
    row = silver('Wisconsin Department of Transportation advisory. '*4+
                 'Silver Alert: Jane Example, age 78, missing in Dane County. Contact police if seen. '*3)
    alert = select_alerts(snapshot([], alerts=[row]), TRAFFIC_NOW, {'dane'})[0]
    text = alert.render(TRAFFIC_NOW, 125)
    assert text.startswith('Silver Alert: Jane Example, age 78, missing in Dane County.')
    assert text.endswith('... Details: 511wi.gov.') and len(text) <= 125
