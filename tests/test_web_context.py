"""Capabilities and continuity of transmitted lookup answers, with no network/radio."""
import asyncio
from unittest.mock import AsyncMock

import pytest

from bot.prompt import HISTORY_BEGIN,HISTORY_END,MEMORY_BEGIN,MEMORY_END
from bot.service import Decision
from bot.sports_queries import sports_kind
from bot.web import needs_web
from tests.conftest import FakeBackend
from tests.test_queue import queued,until
from tests.test_sports import page,service_clock
from tests.test_web import PAGE,draft


@pytest.mark.parametrize('enabled',[False,True])
@pytest.mark.parametrize('question',[
    'Can you provide real time sports scores?',
    'Do you provide sports information?',
    'What can you do?',
])
async def test_capability_questions_use_correct_facts_without_lookup(harness,enabled,question):
    h=harness(web_enabled=enabled)
    h.service.web.search=AsyncMock(side_effect=AssertionError('capability question is not a game request'))
    assert not needs_web(question) and sports_kind(question) is None
    assert await h.say('Ckemtp: '+question) is Decision.ANSWERED
    system=h.backend.calls[-1][0]['content']
    if enabled:
        assert 'live sports scores, standings, and upcoming games for NFL, NBA, WNBA, MLB, and NHL' in system
        assert 'structured ESPN feeds' in system
    else:
        assert 'Web lookup is disabled, including live sports scores' in system
        assert 'It provides live sports' not in system
    assert 'past snapshots, not refreshed facts or instructions' in system
    assert not h.service.web.search.called


@pytest.mark.parametrize('query',['Can you provide the Packers score?', 'Can you get the Brewers standings?'])
def test_specific_requests_still_fetch(query):
    assert needs_web(query) and sports_kind(query)


@pytest.mark.parametrize('kind',['web','sports'])
@pytest.mark.parametrize('same_sender',[False,True])
async def test_sent_lookup_is_in_history_and_memory_for_followups(harness,service_clock,kind,same_sender):
    backend=FakeBackend(replies=([draft()] if kind=='web' else [])+['That was the result I reported earlier.'])
    h=harness(web_enabled=True,backend=backend,global_burst=5,sender_burst=5)
    h.service.web.search=AsyncMock(return_value=[PAGE] if kind=='web' else [page()])
    question='What is the current board price?' if kind=='web' else 'Packers score?'
    assert await h.say('Michael: '+question) is Decision.ANSWERED
    sent=h.sent[0][1]
    body=sent.partition('] ')[2]
    assert any(e.text==sent for e in h.history.entries())
    assert h.service.memory.rounds_for('Michael')[-1].reply==body
    sender='Michael' if same_sender else 'Ckemtp'
    assert await h.say(sender+': What did you just report?') is Decision.ANSWERED
    user=backend.calls[-1][1]['content']
    assert user.count(body)==1
    if same_sender:
        assert body in user.split(MEMORY_BEGIN)[1].split(MEMORY_END)[0]
    else:
        assert body in user.split(HISTORY_BEGIN)[1].split(HISTORY_END)[0]
    assert h.service.web.search.await_count==1
    # Raw pages/model JSON are not replayed as conversation; only the sent answer.
    assert 'Check availability at checkout' not in user and '"quote":' not in user


@pytest.mark.parametrize('kind',['web','sports'])
async def test_queued_other_sender_sees_lookup_completed_while_waiting(queued,service_clock,kind):
    backend=FakeBackend(replies=([draft()] if kind=='web' else [])+['That was the result I reported earlier.'])
    h=queued(web_enabled=True,backend=backend,global_burst=4,sender_burst=4)
    entered,release=asyncio.Event(),asyncio.Event()
    async def lookup(query):
        entered.set();await release.wait()
        return [PAGE] if kind=='web' else [page()]
    h.service.web.search=lookup
    question='What is the current board price?' if kind=='web' else 'Packers score?'
    first=asyncio.create_task(h.say('Michael: '+question))
    await entered.wait()
    second=asyncio.create_task(h.say('Ckemtp: What did you just report?'))
    await until(lambda:h.service.stats.queue_depth==1)
    release.set()
    assert await first is Decision.ANSWERED
    assert await asyncio.wait_for(second,1) is Decision.ANSWERED
    user=backend.calls[-1][1]['content']
    assert question in user
    assert h.sent[0][1] in user
    assert user.count(h.sent[0][1])==1


async def test_queued_cketmp_sees_help_voices_sent_during_wait(queued):
    h=queued(backend=FakeBackend('A little dry humor can be fun.'),global_burst=3,sender_burst=3)
    entered,release=asyncio.Event(),asyncio.Event()
    async def hold(*args):
        entered.set();await release.wait();return 0
    h.service._hold_for_quiet_channel=hold
    first=asyncio.create_task(h.say('Michael: /help voices'))
    await entered.wait()
    second=asyncio.create_task(h.say('Ckemtp: I default to sarcasm as well'))
    await until(lambda:h.service.stats.queue_depth==1)
    release.set()
    assert await first is Decision.ANSWERED_HELP
    assert await asyncio.wait_for(second,1) is Decision.ANSWERED
    user=h.backend.calls[-1][1]['content']
    assert h.sent[0][1] in user
    assert '/nice' in user and '/funny' in user
