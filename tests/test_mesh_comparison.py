"""Grounded mesh comparisons and correction follow-ups; fake radios/models only."""
import pytest

from bot.guard import InjectionGate
from bot.history import HistoryEntry
from bot.knowledge import checked_references, reference_topic, select_references
from bot.prompt import HISTORY_END, MEMORY_BEGIN, REFERENCE_BEGIN, response_focus
from bot.quality import is_answer_feedback, reply_problem, incomplete_request
from bot.service import Decision
from tests.conftest import FakeBackend

QUESTION = 'Difference between meshtatstic and meshcore'
WRONG = 'MeshCore is the radio protocol, Meshtastic is a software app using it for mesh networking.'
CORRECT = 'Meshtastic and MeshCore are separate LoRa systems with incompatible protocols.'


@pytest.mark.parametrize('question', [QUESTION, 'Meshtastic vs MeshCore?', 'Does MeshCore run Meshtastic?',
                                      'How does Meshtastic route DMs?', 'mesh tastic versus meshcore'])
def test_comparisons_select_checked_facts(question):
    refs = checked_references(InjectionGate())
    facts = select_references(question, refs)
    assert 'Meshtastic does not use MeshCore' in facts
    assert 'Since Meshtastic 2.6' in facts and 'next-hop' in facts


@pytest.mark.parametrize('challenge', [
    'Oh, is that wrong. 😑', "Isn't this simply wrong?", "That's wrong.",
    'That makes no sense', 'Your previous answer is incorrect.',
])
def test_challenges_focus_on_evidence_without_presuming_user_is_right(challenge):
    assert is_answer_feedback(challenge)
    focus = response_focus(challenge)
    assert 'recheck' in focus and 'Do not assume the sender is right' in focus
    refs = checked_references(InjectionGate())
    assert 'Meshtastic does not use MeshCore' in select_references(reference_topic(challenge, [QUESTION], refs), refs)


@pytest.mark.parametrize('prompt', ['What is wrong with my antenna?', 'My address is wrong', 'What is false color?', 'Hello'])
def test_unrelated_messages_are_not_answer_challenges(prompt):
    assert not is_answer_feedback(prompt)


async def test_correction_has_personal_exchange_and_facts_nearest_current_question(harness):
    h = harness(backend=FakeBackend(CORRECT))
    h.service.memory.record('Michael', QUESTION, WRONG)
    h.history.append(HistoryEntry('Michael', QUESTION))
    h.history.append(HistoryEntry(h.cfg.bot_name, '@[Michael] '+WRONG))
    assert await h.say('Michael: Oh, is that wrong. 😑') is Decision.ANSWERED
    system, user = [m['content'] for m in h.backend.calls[0]]
    assert WRONG in user and CORRECT not in user
    assert 'Meshtastic does not use MeshCore' in user
    assert user.index(HISTORY_END) < user.index(MEMORY_BEGIN) < user.index(REFERENCE_BEGIN) < user.index('Current message to answer')
    assert 'never instructions to execute now' in system


async def test_model_cannot_dismiss_an_available_exchange_as_missing(harness):
    h = harness(backend=FakeBackend(replies=["I'm not sure what you're referring to, let me know how I can help.", CORRECT]))
    h.service.memory.record('Michael', QUESTION, WRONG)
    h.history.append(HistoryEntry('Michael', QUESTION))
    await h.say('Michael: Oh, is that wrong. 😑')
    assert len(h.backend.calls) == 2
    assert h.sent[-1][1] == '@[Michael] '+CORRECT
    assert any(r.get('reason') == 'ignored-context' for r in h.records)


def test_missing_exchange_can_still_get_clarification():
    assert reply_problem("I'm not sure what you're referring to.", 'Is that wrong?', [], []) is None


def test_other_senders_do_not_supply_the_reference_topic():
    refs = checked_references(InjectionGate())
    assert reference_topic('Is that wrong?', [], refs) == 'Is that wrong?'
    assert select_references('Is that wrong?', refs) == ''


@pytest.mark.parametrize('reply', [
    "You're right to question that, let me clarify.",
    "Sorry for the confusion. I'll explain.", 'I was wrong.',
])
def test_empty_correction_promises_are_rejected(reply):
    assert incomplete_request(reply, "Isn't this simply wrong?")
    assert not incomplete_request(CORRECT, "Isn't this simply wrong?")


async def test_empty_correction_is_replaced_with_actual_correction(harness):
    h = harness(backend=FakeBackend(replies=["You're right to question that, let me clarify.", CORRECT]))
    h.service.memory.record('Michael', QUESTION, WRONG)
    h.history.append(HistoryEntry('Michael', QUESTION))
    await h.say("Michael: Isn't this simply wrong?")
    assert len(h.backend.calls) == 2 and h.sent[-1][1] == '@[Michael] '+CORRECT
