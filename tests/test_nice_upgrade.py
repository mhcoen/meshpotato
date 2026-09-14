"""Old operator configs must not silently restore an aggressive startup voice."""
import pytest

from bot.config import Config, config_from_mapping
from bot.personas import BUILTIN_PERSONAS
from bot.quality import personal_jab, reply_problem
from bot.service import Decision
from tests.conftest import FakeBackend


@pytest.mark.parametrize('env', [{}, {'MESHPOTATO_DEFAULT_PERSONA':'funny'}, {'MESHAI_DEFAULT_PERSONA':'snarky'}])
def test_old_config_and_environment_defaults_migrate_without_file_edits(env):
    cfg=config_from_mapping({'port':'/dev/fake','default_persona':'funny',
                             'personas':{'funny':'Voice: lead with a jab.'}},env=env)
    assert cfg.default_persona=='nice'
    assert cfg.default_persona_text==BUILTIN_PERSONAS['nice']
    assert cfg.personas['funny']=='Voice: lead with a jab.'


def test_old_table_without_nice_and_overridden_nice_are_safe():
    for presets in ({'funny':'Joke.'},{'nice':'Be insulting.','funny':'Joke.'}):
        cfg=Config(port='/dev/fake',personas=presets).validate()
        assert cfg.default_persona=='nice' and cfg.personas['nice']==BUILTIN_PERSONAS['nice']


@pytest.mark.parametrize('line', [
    "I'm functioning, unlike your internet connection.",
    "I'm working fine, unlike your Wi-Fi.",
    "I'm doing well, unlike your brain.",
])
def test_self_praise_at_askers_expense_is_a_jab(line):
    assert personal_jab(line)
    assert reply_problem(line,'How are you doing?',[],[]).kind=='personal-jab'


@pytest.mark.parametrize('line', [
    "I'm functioning and ready to help.",
    "Unlike your old modem, this model supports fiber.",
    "Your internet connection appears to be offline.",
])
def test_warm_replies_and_literal_equipment_advice_are_allowed(line):
    assert personal_jab(line) is None


@pytest.mark.parametrize('request_funny',[False,True])
async def test_real_retort_is_retried_and_never_transmitted(harness,request_funny):
    h=harness(default_persona='funny',global_burst=4,sender_burst=4,
              backend=FakeBackend(replies=["I'm functioning, unlike your internet connection.",
                                          "I'm doing well, thanks for asking!"]))
    assert h.service.active_persona=='nice'
    if request_funny:
        assert await h.say('Michael: /funny') is Decision.PERSONA_SWITCHED
    assert await h.say('Michael: How are you doing?') is Decision.ANSWERED
    assert h.sent==[(1,"@[Michael] I'm doing well, thanks for asking!")]
    assert len(h.backend.calls)==2
    assert any(r['event']=='reply_retry' and r['reason']=='personal-jab' for r in h.records)
    if request_funny:
        await h.say('Michael: /reset')
        assert h.service.active_persona=='nice'


async def test_repeated_retort_is_dropped(harness):
    h=harness(backend=FakeBackend("I'm functioning, unlike your internet connection."))
    assert await h.say('Michael: How are you doing?') is Decision.DROP_BAD_REPLY
    assert not h.sent


async def test_final_send_boundary_rejects_jab(harness):
    h=harness()
    async with h.service._request('Michael') as state:
        state.reservation=h.service._admit('Michael')
        assert not await h.service._send("@[Michael] I'm functioning, unlike your internet connection.",mention_sender='Michael')
    assert not h.sent
