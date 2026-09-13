"""Regressions from the five-league audit, with captured ESPN field shapes."""
import json
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock
from urllib.parse import urlsplit, parse_qs

import pytest
from meshcore import EventType

from bot import sports_details as details
from bot.sports import _team, matches, sports_answer
from bot.sports_queries import sports_kind, with_team_context
from bot.service import Decision
from tests.conftest import FakeBackend
from tests.test_sports import NOW, service_clock
from tests.test_sports_details import standing, next_page, fake_fetch, scheduled


def feed(league):
    return json.loads((Path(__file__).parent/'fixtures'/'sports'/f'{league}.json').read_text())


def fixture_fetch(url):
    league = url.split('/sports/')[1].split('/')[1]
    data = feed(league)
    if '/teams?' in url:
        rows = [r for g in data['children'] for r in g['standings']['entries']]
        return {'sports':[{'leagues':[{'slug':league,'teams':[{'team':r['team']} for r in rows]}]}]}
    if '/standings?' in url:
        return data
    raise AssertionError(url)


@pytest.mark.parametrize('league,query,expected', [
    ('mlb','What place are the Brewers in?', 'Brewers: listed 1st in NL Central, 93-57; lead by 10 games'),
    ('nfl',"Who's leading the AFC North?", 'in AFC North'),
    ('nba','What place are the Knicks in?', 'Knicks: listed 2nd in NBA Atlantic, 53-29; 3 games behind'),
    ('nhl','What place are the Bruins in?', 'Bruins:'),
    ('wnba','What place are the Atlanta Dream in?', 'Dream:'),
])
def test_real_five_league_collection_and_answers(league,query,expected):
    # Captured NBA/NHL results are deliberately replayed inside their season;
    # this tests schema support, not the historical correctness of that snapshot.
    now = NOW.replace(month=4) if league in {'nba','nhl'} else NOW
    pages = details.collect_details(query,fixture_fetch,now)
    answer,evidence = sports_answer(pages,query,now,140)
    assert evidence, (pages,answer)
    assert expected in answer
    assert evidence['league'] == league
    if league == 'nhl':
        assert 'points' in answer and 'games behind' not in answer
        assert '45-27-10; 100 points' in answer
    if league == 'nfl':
        assert len(pages) == 1 and 'NFC' not in answer


@pytest.mark.parametrize('alias',['otLosses','overtimeLosses','OTLosses'])
def test_overtime_aliases_and_conflicts(alias):
    r=deepcopy(feed('nhl')['children'][0]['standings']['entries'][0])
    r['stats']=[s for s in r['stats'] if s['name'] not in details.OT_NAMES]
    r['stats'].append({'name':alias,'value':9})
    assert details.stats(r,'nhl')['OTLosses']==9
    r['stats'].append({'name':'otLosses','value':10})
    with pytest.raises(ValueError): details.stats(r,'nhl')


def test_unsorted_group_is_sorted_and_equal_statistics_share_rank():
    p=standing();p['group']['standings']['entries'].reverse()
    assert 'listed 1st' in sports_answer([p],'Brewers standings?',NOW,140)[0]
    rows=p['group']['standings']['entries']
    rows[1]['stats']=deepcopy(rows[2]['stats'])
    answer,evidence=sports_answer([p],'Cubs standings?',NOW,140)
    assert evidence and 'tied 1st' in answer


def test_conference_position_is_computed_across_divisions():
    data=feed('mlb')
    rows=[r for g in data['children'] if g['name'].startswith('National League') for r in g['standings']['entries']]
    p=standing();p['group']={'name':'National League','standings':{'entries':rows[::-1]}}
    answer,evidence=sports_answer([p],'What place are the Brewers in the National League?',NOW,140)
    assert evidence and '1st in NL' in answer
    assert 'playoff' not in answer


@pytest.mark.parametrize('league',['mlb','nfl','nba','nhl','wnba'])
def test_offseason_or_missing_dates_cannot_be_current(league):
    data=feed(league);g=data['children'][0];team=g['standings']['entries'][0]['team']['displayName']
    end=datetime.fromisoformat(data['season']['endDate'].replace('Z','+00:00'))
    now=end+timedelta(days=1)
    p={'sports_detail':'standings','league':league,'season':2026,'season_info':data['season'],
       'group':details._compact_group(g),'fetched_at':now.isoformat()}
    assert sports_answer([p],f'{team} standings?',now,140)[1] is None
    p['season_info']={}
    assert sports_answer([p],f'{team} standings?',NOW,140)[1] is None


def test_early_season_team_has_no_invented_position_or_gap():
    p=standing()
    for s in p['group']['standings']['entries'][0]['stats']:
        if s['name'] in {'wins','losses','ties','OTLosses','winPercent'}: s['value']=0
    # MLB also carries an unrelated OTLosses field; it must not count as games played.
    p['group']['standings']['entries'][0]['stats'].append({'name':'OTLosses','value':6})
    answer,evidence=sports_answer([p],'How are the Brewers doing?',NOW,140)
    assert evidence and 'no completed games recorded' in answer
    assert 'behind' not in answer and 'listed' not in answer


CHAT = [
    'how are they doing in school', 'did they win the lawsuit', 'how far behind is my package',
    "how's the sun doing today, brutal heat", 'how are the bulls doing in the market',
    'how are the reds doing on my radio', 'how are the wings doing at the bar',
    'How are they doing?', 'When do they play next?',
]


@pytest.mark.parametrize('query',CHAT)
async def test_ordinary_chat_uses_model_without_search(query,harness):
    assert sports_kind(query) is None
    h=harness(web_enabled=True, backend=FakeBackend("I need a little more context to answer that."))
    h.service.web.search=AsyncMock(side_effect=AssertionError('ordinary chat must not fetch'))
    assert await h.say('Michael: '+query) is Decision.ANSWERED
    assert h.backend.calls and not h.service.web.search.called


@pytest.mark.parametrize('query',CHAT[:7])
def test_unrelated_chat_does_not_borrow_existing_context(query):
    assert with_team_context(query,{'team':'Milwaukee Brewers','league':'mlb'})==query


@pytest.mark.parametrize('query,kind',[
    ('are the Brewers in first place','standings'), ('what time is the Packers game','next'),
    ("who's winning the Bucks game",'score'), ('How are the Heat doing?','overview'),
    ('How are the miami heat doing?','overview'), ('How are the heat doing in the NBA?','overview'),
])
def test_missing_phrasings_and_qualified_common_nicknames(query,kind):
    assert sports_kind(query)==kind


def test_english_abbreviations_are_case_sensitive():
    saints={'name':'Saints','displayName':'New Orleans Saints','shortDisplayName':'Saints','abbreviation':'NO','location':'New Orleans'}
    team=_team({'team':saints})
    assert not matches(team,'How many games behind are the Cubs? No idea')
    assert matches(team,'NO score?')
    assert matches(team,'new orleans score?')
    pages=details.collect_details('How many games behind are the Cubs? No idea',fixture_fetch,NOW)
    assert sports_answer(pages,'How many games behind are the Cubs? No idea',NOW,140)[1]


async def test_failed_send_and_failed_lookup_do_not_leave_sports_context(harness,service_clock):
    h=harness(web_enabled=True,global_burst=5,sender_burst=5)
    h.service.web.search=AsyncMock(return_value=[standing()])
    h.mc.commands.send_result_type=EventType.ERROR
    assert await h.say('Michael: Brewers standings?') is Decision.DROP_SEND_FAILED
    assert not h.service._sports_context
    h.mc.commands.send_result_type=EventType.OK
    assert await h.say('Michael: Brewers standings?') is Decision.ANSWERED
    assert h.service._sports_context
    h.service.web.search=AsyncMock(return_value=[])
    await h.say('Michael: Packers standings?')
    assert not h.service._sports_context


def test_doubleheader_next_game_is_earliest_and_dst_uses_target_date():
    from zoneinfo import ZoneInfo
    now=NOW.replace(month=10,day=31,tzinfo=ZoneInfo('America/Chicago'))
    first=scheduled();second=scheduled()
    first['competitions'][0]['date']='2026-11-02T19:00Z'
    second['competitions'][0]['date']='2026-11-02T23:00Z'
    def fetch(url):
        if '/teams/8' in url:
            from tests.test_sports_details import BREWERS
            return {'team':{**BREWERS,'nextEvent':[second,first]}}
        return fake_fetch(url)
    pages=details.collect_details('When do the Brewers play next?',fetch,now)
    answer,evidence=sports_answer(pages,'When do the Brewers play next?',now,140)
    assert evidence and '13:00 CST' in answer


def test_season_opening_uses_feed_dates_not_month_cutoff():
    # NHL can open in September while the initial heuristic still selects 2026.
    calls=[]
    old=feed('nhl');new=deepcopy(old)
    new['season']={'year':2027,'startDate':'2026-09-20T07:00Z','endDate':'2027-07-01T06:59Z'}
    def fetch(url):
        if '/standings?' in url:
            year=int(parse_qs(urlsplit(url).query)['season'][0]);calls.append(year)
            return new if year==2027 else old
        return fixture_fetch(url)
    now=NOW.replace(day=21)
    pages=details.collect_details('Bruins standings?',fetch,now)
    assert calls==[2026,2027]
    assert sports_answer(pages,'Bruins standings?',now,140)[1]


def test_offseason_collection_does_not_emit_final_table():
    now=NOW.replace(month=12,day=1)
    pages=details.collect_details('Brewers standings?',fixture_fetch,now)
    assert not sports_answer(pages,'Brewers standings?',now,140)[1]


def test_fallback_includes_game_exactly_fourteen_days_away():
    urls=[]
    def fetch(url):
        urls.append(url)
        if '/teams/8' in url:
            from tests.test_sports_details import BREWERS
            return {'team':{**BREWERS,'nextEvent':[]}}
        if 'scoreboard?' in url:
            events=[scheduled(offset=14)] if 'dates=20260927' in url else []
            return {'leagues':[{'slug':'mlb'}],'events':events}
        return fake_fetch(url)
    query='When do the Brewers play next?'
    pages=details.collect_details(query,fetch,NOW)
    assert sports_answer(pages,query,NOW,140)[1]
    assert any('dates=20260927' in u for u in urls)


async def test_forget_during_sports_retrieval_prevents_context_restoration(harness,service_clock):
    import asyncio
    h=harness(web_enabled=True,global_burst=5,sender_burst=5)
    started,finish=asyncio.Event(),asyncio.Event()
    async def lookup(query):
        started.set();await finish.wait();return [standing()]
    h.service.web.search=lookup
    task=asyncio.create_task(h.say('Michael: Brewers standings?'))
    await started.wait()
    # Forget applies immediately even when its acknowledgement cannot take the active slot.
    assert await h.say('Michael: /forget') is Decision.DROP_RATE_LIMITED
    assert h.service._requests[task].remember is False
    finish.set();await task
    assert not h.service._sports_context
