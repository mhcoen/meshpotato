"""User-facing discovery, follow-ups and no-game evidence; all I/O is fake."""
from copy import deepcopy
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from bot import sports
from bot.history import HistoryEntry
from bot.knowledge import load_references, reference_topic, select_references
from bot.personas import command_parts
from bot.service import Decision
from tests.conftest import FakeBackend
from tests.test_sports import NOW, QUERY, event


@pytest.mark.parametrize('text,expected', [
    ('!roll 3 8', ('roll', '3 8')), ('/roll 3 8', ('roll', '3 8')),
    ('!!roll 3 8', ('roll', '3 8')), ('help', ('help', '')),
    ('Help voices?', ('help', 'voices')), ('What can you do?', ('help', '')),
    ('How do I use this bot?', ('help', '')), ('help me write a poem', (None, '')),
    ('That would help', (None, '')),
])
def test_aliases_preserve_arguments_and_ordinary_requests(text, expected):
    assert command_parts(text, '!!') == expected


@pytest.mark.parametrize('prefix', ['!', '/', '!!'])
async def test_alias_command_arguments_reach_dice_handler(harness, prefix):
    h = harness(command_prefix='!!')
    assert await h.say(f'Alice: {prefix}roll 3 8') is Decision.ANSWERED_ROLL
    values = h.sent[0][1].removeprefix('@[Alice] Rolled ').rstrip('.').split(', ')
    assert len(values) == 3 and all(1 <= int(n) <= 8 for n in values)
    assert not h.backend.calls


@pytest.mark.parametrize('command', ['help', '!help', '/help', 'what can you do?'])
async def test_discovery_advertises_live_topics(harness, command):
    h = harness(web_enabled=True)
    assert await h.say('Alice: '+command) is Decision.ANSWERED_HELP
    assert all(word in h.sent[0][1] for word in ('weather', 'Packers record', 'Brewers next game', 'traffic'))
    assert not h.backend.calls


@pytest.mark.parametrize('prefix', ['!', '/'])
async def test_about_and_topics_make_model_and_source_discoverable(harness, prefix):
    h = harness(global_burst=3, sender_burst=3)
    assert await h.say(f'Alice: {prefix}help topics') is Decision.ANSWERED_HELP
    assert 'about' in h.sent[-1][1]
    assert await h.say(f'Alice: {prefix}about') is Decision.ANSWERED_HELP
    assert h.cfg.model in h.sent[-1][1]
    assert await h.say(f'Alice: {prefix}about source') is Decision.ANSWERED_HELP
    assert h.sent[-1][1] == '@[Alice] https://github.com/mhcoen/meshpotato'
    assert not h.backend.calls


@pytest.mark.parametrize('command', ['!weather', '/weather', '!wx', '/wx'])
async def test_weather_aliases_use_actual_weather_handler(harness, command):
    from tests.test_weather import pages
    h = harness(web_enabled=True)
    h.service.web.search = AsyncMock(return_value=pages(datetime.now().astimezone()))
    assert await h.say(f'Alice: {command} Madison, WI') is Decision.ANSWERED
    assert 'Madison, WI' in h.sent[-1][1] and 'H:73' in h.sent[-1][1]
    assert not h.backend.calls
    assert h.service.web.search.await_count == 1


@pytest.mark.parametrize('command', ['!traffic', '/traffic'])
async def test_traffic_aliases_use_cache(harness, command):
    from tests.test_traffic import cache
    h = harness(web_enabled=True, traffic_enabled=True)
    h.service.traffic = cache(h.clock)
    h.service.web.search = AsyncMock(side_effect=AssertionError('must use cache'))
    assert await h.say(f'Alice: {command} on the Beltline') is Decision.ANSWERED
    assert 'traffic' in h.sent[-1][1].lower()
    assert not h.backend.calls
    h.service.web.search.assert_not_awaited()


async def test_direct_reply_guidance_and_quiet_recovery(harness, clock):
    h = harness(global_burst=10, sender_burst=10)
    for _ in range(6):
        assert await h.say('Alice: @[MeshAI] !help') is Decision.ANSWERED_HELP
    assert await h.say('Alice: @[MeshAI] !about') is Decision.ANSWERED_HELP
    assert 'wait a minute' in h.sent[-1][1]
    assert await h.say('Alice: @[MeshAI] !help') is Decision.DROP_LOOP_GUARD
    clock.advance(60)
    assert await h.say('Alice: @[MeshAI] !help') is Decision.ANSWERED_HELP
    assert h.sent[-1][1] == h.cfg.help_message


async def test_ambiguous_guidance_is_not_retried(harness):
    h = harness(global_burst=10, sender_burst=10)
    for _ in range(6):
        await h.say('Alice: @[MeshAI] !help')
    h.mc.commands.raise_on_send = RuntimeError('ambiguous radio failure')
    assert await h.say('Alice: @[MeshAI] !help') is Decision.DROP_SEND_FAILED
    assert await h.say('Alice: @[MeshAI] !help') is Decision.DROP_LOOP_GUARD
    assert h.service.stats.send_errors == 1
    assert h.limiter.snapshot()['global_tokens'] == 3


def empty_feed(url):
    league = url.split('/')[-2]
    if '/teams?' in url:
        return {'sports': [{'leagues': [{'slug': league, 'teams':
            [{'team': t['team']} for t in event()['competitions'][0]['competitors']]
            if league == 'nfl' else []}]}]}
    return {'leagues': [{'slug': league}], 'events': []}


def test_verified_empty_scoreboards_produce_useful_no_game_answer():
    packet = sports.collect_scores(QUERY, empty_feed, NOW)
    answer, evidence = sports.score_answer(packet, QUERY, NOW, 130)
    assert answer == 'No matching Packers game found for 09/13. Try a game date or ask for their next game.'
    assert evidence['lookup_result'] == 'no-matching-game'
    assert evidence['team_context'] == {'team': 'Green Bay Packers', 'league': 'nfl'}


@pytest.mark.parametrize('failure', ['offline', 'malformed', 'invalid-score', 'empty-competition', 'too-many'])
def test_no_game_requires_complete_valid_coverage(failure):
    def fetch(url):
        data = empty_feed(url)
        if 'scoreboard?' in url and 'dates=20260913' in url:
            if failure == 'offline':
                raise OSError('offline')
            if failure == 'malformed':
                return {'events': []}
            e = event()
            if failure == 'empty-competition':
                e['competitions'] = []
            elif failure == 'invalid-score':
                e['competitions'][0]['competitors'][0]['score'] = 'garbage'
            data['events'] = [e] * (100 if failure == 'too-many' else 1)
        return data
    packet = sports.collect_scores(QUERY, fetch, NOW)
    assert sports.score_answer(packet, QUERY, NOW, 130) == (sports.UNAVAILABLE, None)
    assert all('sports_error' in p for p in packet)


@pytest.mark.parametrize('mutation', [
    lambda p: p.update(fetched_at=(NOW-timedelta(seconds=61)).isoformat()),
    lambda p: p.update(date='2026-09-12'),
    lambda p: p.update(league='nba'),
    lambda p: p.update(team=event()['competitions'][0]['competitors'][0]['team']),
])
def test_no_game_packet_is_revalidated(mutation):
    packet = deepcopy(sports.collect_scores('NFL Packers score', empty_feed, NOW))
    mutation(packet[0])
    assert sports.score_answer(packet, 'NFL Packers score', NOW, 130) == (sports.UNAVAILABLE, None)


def test_referential_radio_followup_selects_fixed_reference_without_changing_question():
    refs = load_references()
    previous = 'A hash held for 24 hours would simply eliminate them'
    topic = reference_topic('I get that, I am considering whether they should', [previous], refs)
    assert 'bounded circular cache' in select_references(topic, refs)
    assert reference_topic('What should I bake?', [previous], refs) == 'What should I bake?'
    assert reference_topic('What is SNR?', [previous], refs) == 'What is SNR?'


async def test_followup_gets_reference_from_same_sender_only(harness):
    h = harness(backend=FakeBackend('Longer retention can reduce replays but consumes more memory.'))
    h.history.append(HistoryEntry('Alice', 'Should repeaters hold packet hashes for a day?'))
    h.history.append(HistoryEntry('Bob', 'What is SNR?'))
    assert await h.say('Alice: Would that be worth doing?') is Decision.ANSWERED
    user = h.backend.calls[0][1]['content']
    assert 'bounded circular cache' in user
    assert 'Current message to answer' in user and 'Would that be worth doing?' in user


@pytest.mark.parametrize('prompt', [
    'What is sarcasm?', 'Tell me a joke about debugging', 'Can you debug my code?',
    'My bank declined my card', 'Why did the compiler ignore this flag?',
])
def test_conversation_focus_does_not_hijack_ordinary_topics(prompt):
    from bot.prompt import response_focus
    assert response_focus(prompt) == ''


async def test_feedback_keeps_current_request_after_untrusted_context(harness):
    h = harness(backend=FakeBackend('Thanks for checking and telling me.'))
    h.service.memory.record('Alice', 'Did you reply?', 'I definitely replied.')
    h.history.append(HistoryEntry('Alice', 'Please investigate the silence.'))
    assert await h.say('Alice: I checked your logs, you ignored my question.') is Decision.ANSWERED
    system, user = [m['content'] for m in h.backend.calls[0]]
    assert user.index('I definitely replied.') < user.index('Current message to answer')
    assert 'without recorded evidence' in system
    assert 'Never defend a previous answer' in user


@pytest.mark.parametrize('sender,include', [('Michael', True), ('Michael with a long sender name', False), ('🌟'*8, False)])
async def test_about_readme_link_respects_complete_packet_budget(harness, sender, include):
    h = harness(bot_name='Mesh Potato', reply_max_chars=147,
                model='qwen3:30b-a3b-instruct-2507-q4_K_M')
    assert await h.say(f'{sender}: /about') is Decision.ANSWERED_HELP
    text = h.sent[-1][1]
    url = 'https://github.com/mhcoen/meshpotato/blob/main/README.md'
    assert (url in text) is include
    assert h.cfg.model in text
    assert len(('Mesh Potato: '+text).encode()) <= 160
    assert len(text) <= 147


@pytest.mark.parametrize('command', ['about', '/about', '!about'])
async def test_about_entry_points_include_readme_when_it_fits(harness, command):
    h = harness()
    assert await h.say('Alice: '+command) is Decision.ANSWERED_HELP
    assert h.cfg.model in h.sent[-1][1]
    assert h.sent[-1][1].endswith('https://github.com/mhcoen/meshpotato/blob/main/README.md')
    assert not h.backend.calls


@pytest.mark.parametrize('command,decision', [('!about', Decision.ANSWERED_HELP), ('!roll 3 8', Decision.ANSWERED_ROLL), ('!pirate', Decision.PERSONA_SWITCHED)])
async def test_bang_command_immediately_after_reply_mention(harness, command, decision):
    h = harness()
    assert await h.say('Alice: @[MeshAI]'+command) is decision
    assert not h.backend.calls
    await h.service.stop()


async def test_radio_followup_recovery_keeps_inherited_topic(harness):
    answer = "Hash retention works like a radio's memory, but costs RAM."
    h = harness(backend=FakeBackend(replies=['PASS', 'PASS', answer]))
    h.history.append(HistoryEntry('Alice', 'Should repeaters keep packet hashes for a day?'))
    assert await h.say('Alice: Would that be worth doing?') is Decision.ANSWERED_RECOVERY
    assert h.sent[-1][1] == '@[Alice] '+answer
