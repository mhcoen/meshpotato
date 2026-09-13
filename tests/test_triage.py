"""Inbound triage: what needs no answer when the bot answers the whole channel."""

import pytest

from bot.triage import is_reaction, mentions_someone


@pytest.mark.parametrize("text", [
    "lol", "Lol k", "Lmao", "haha nice", "ok", "ty", "thanks", "Wtf", "Omg", "LOLOL", "hahahaha", "xD",
    "\U0001f602", "\U0001f605", "Omg \U0001f633 \U0001f602", "Wtf \U0001f602", "...", "!!!", "\U0001f44d\U0001f44d",
])
def test_reactions_have_nothing_to_answer(text):
    assert is_reaction(text)


@pytest.mark.parametrize("text", [
    "hi", "Hello?", "Test", "Lol what", "Sweet christ", "yes", "no", "Chill potato.", "What time is it?",
    "ok but why", "Are you still there?", "thanks for the help, what was the SNR", "lol lol lol lol",
])
def test_questions_and_remarks_are_not_reactions(text):
    assert not is_reaction(text)


def test_mentions_mark_a_line_for_someone_else():
    assert mentions_someone("Lol ty @[Fadepoint Tag]")
    assert mentions_someone('Repeat "@[Michael] Your antenna is fine."')
    assert not mentions_someone("email me at a@b.c")
    assert not mentions_someone("what does [80] mean")


def test_a_mention_of_the_bot_itself_is_for_the_bot():
    assert not mentions_someone("Hey @[Mesh Potato], can you hear me?", "Mesh Potato")
    assert mentions_someone("Hey @[Mesh Potato], tell @[Bob] hi", "Mesh Potato")
    assert mentions_someone("Hey @[Mesh Potato], can you hear me?", "Other Bot")
    assert mentions_someone("half a mention @[Mesh Potato", "Mesh Potato")


def test_questions_and_other_scripts_are_never_reactions():
    assert not is_reaction("You ok?") and not is_reaction("ok?")
    assert not is_reaction("\u4f60\u597d\uff1f") and not is_reaction("\u0421\u043b\u044b\u0448\u0438\u0448\u044c")
    assert is_reaction("Thank you!") and is_reaction("thank you so much") is False
