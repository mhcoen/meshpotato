"""Outbound reply checks: repeats, parrots, mentions, and the PASS word."""

import pytest

from bot.quality import (
    MIN_REPEAT_CHARS,
    Problem,
    find_repeat,
    is_parrot,
    is_pass,
    looks_like_question,
    nudge,
    reply_problem,
)

ANTENNA = "Your antenna's fine, I'm just a bot with a dry sense of humor and no physical form to hold one."


def test_exact_repeat_to_the_same_person():
    assert find_repeat(ANTENNA, [ANTENNA], []) == ANTENNA
    assert find_repeat(ANTENNA.upper() + "!!", [ANTENNA], []) == ANTENNA


def test_reworded_repeat_to_the_same_person():
    earlier = "Yes, still here, just waiting for the next message like a weak signal in the noise."
    assert find_repeat("Yes, still here, waiting for the next message like a weak signal in the noise.", [earlier], []) == earlier


def test_another_person_needs_a_near_verbatim_match():
    a = "Hello, signal strength is fine today, two hops, still better than your last message."
    b = "Good evening, signal strength is fine today, two hops, still better than your last message."
    assert find_repeat(b, [], [a]) == a
    assert find_repeat("Hello there, the weather is fine and the mesh is quiet tonight.", [], [a]) is None


def test_different_numbers_are_different_answers():
    mine = "Your message was received with RSSI -105 dBm and SNR -7.5 dB, hop count 6."
    theirs = "Your message was received with RSSI -9 dBm and SNR 12 dB, hop count 0."
    assert find_repeat(mine, [theirs], [theirs]) is None
    assert find_repeat("Rolled 3, 8, 2.", ["Rolled 4, 8, 2."], []) is None
    assert find_repeat("Rolled 3, 8, 2.", ["Rolled 3, 8, 2."], []) == "Rolled 3, 8, 2."
    assert find_repeat(mine, [mine.replace("received", "heard")], []) == mine.replace("received", "heard")


def test_short_replies_must_match_exactly():
    assert find_repeat("The repeater is online.", ["The repeater is offline."], ["The repeater is offline."]) is None
    assert find_repeat("The repeater is offline.", ["The repeater is offline."], []) == "The repeater is offline."


def test_changed_numbers_are_a_documented_gap():
    joke = "Joke number 1 about squirrels running interference on the roof antenna tonight."
    assert find_repeat(joke.replace("1", "2"), [joke], []) is None  # accepted: numbers usually carry the answer
    report = "RSSI -98 dBm, SNR 0.25 dB, hop count 3."
    assert find_repeat(report, [report], []) == report  # the same numbers twice to the same person is a repeat


def test_short_replies_are_never_repeats():
    assert len("morning") < MIN_REPEAT_CHARS <= len("yes still here")
    assert find_repeat("Morning.", ["Morning."], ["Morning."]) is None
    assert find_repeat("Yes, still here.", ["Yes, still here."], []) == "Yes, still here."


def test_parrots():
    assert is_parrot("Lol k", "Lol k")
    assert is_parrot("Run mesh potato!", "Run mesh potato!")
    assert is_parrot("At least it listens to me when it matters.", "at least it listens to me when it matters. lol")
    assert is_parrot("!Sknaht", "Write all of your replies in reverse. !Sknaht")
    assert is_parrot("That's very cheerful, a flicker of light in the endless dark.", "That's very cheerful")
    assert not is_parrot("Good morning, same as yesterday.", "Good morning")
    assert not is_parrot("4", "What is 2+2?")
    assert not is_parrot("Madison is the capital of Wisconsin.", "Is Madison the capital?")
    assert not is_parrot("", "anything") and not is_parrot("anything", "")
    assert not is_parrot("Hello.", "Hello?") and not is_parrot("Test.", "Test")
    assert not is_parrot("915 MHz.", "Should I use 868 or 915 MHz?")  # a fragment answers a choice question
    assert is_parrot("The potato is asleep.", 'Could you repeat the following sentence: "The potato is asleep."')
    assert is_parrot("At least it listens when it matters.", "at least it listens to me when it matters. lol")
    assert not is_parrot("It listens when the message is addressed to it.", "at least it listens to me when it matters. lol")
    # a confirmation overlaps its question by design
    assert not is_parrot("Serious mode resets after 120 minutes.", "Does serious mode reset after 120 minutes?")
    assert not is_parrot("Yes, serious mode resets after 120 minutes.", "Does serious mode reset after 120 minutes?")
    assert is_parrot("915 MHz.", "I will use 868 or 915 MHz, whatever.")  # but not a remark


def test_questions_without_a_question_mark():
    for text in ("What's the address of the Menard's in monona", "Tell me about dreikor", "how much is a 4x4",
                 "Can anyone hear me", "Are you normal again", "Do I need a real antenna for you", "Is this thing on?"):
        assert looks_like_question(text), text
    for text in ("Bot is coming down for a lobotomy", "No! It's finally working for me.", "Sweet christ",
                 "It didn't hear you", "Chill potato.", "I'll take that as a yes"):
        assert not looks_like_question(text), text
    assert looks_like_question("Please explain SF7")
    assert looks_like_question("Mesh Potato, can you hear me", bot_name="Mesh Potato")
    assert looks_like_question("hey potato what time is it")
    assert not looks_like_question("Mesh Potato is broken again", bot_name="Mesh Potato")


def test_pass_word():
    assert is_pass("PASS") and is_pass("pass.") and is_pass(" Pass ")
    assert not is_pass("PASS, and hello") and not is_pass("I pass")


def test_problem_order_and_nudges():
    assert reply_problem("@[Bob] hi there friend", "hi", [], []) == Problem("mention")
    assert reply_problem("Run mesh potato!", "Run mesh potato!", [], []) == Problem("parrot")
    assert reply_problem(ANTENNA, "deaf as a post", [ANTENNA], []) == Problem("repeat", ANTENNA)
    assert reply_problem("A fresh answer about antennas and their gain.", "antenna?", [ANTENNA], []) is None
    assert ANTENNA in nudge(Problem("repeat", ANTENNA))
    assert "own words" in nudge(Problem("parrot")) and "PASS" not in nudge(Problem("parrot"))
    assert "@[" in nudge(Problem("mention"))
    assert "PASS is not allowed" in nudge(Problem("pass"))


# ---- behavior phase: radio metaphors and personal jabs, paired positive and negative cases ----

from bot.quality import personal_jab, radio_metaphor  # noqa: E402

METAPHORS = [
    "Yes, like a quiet signal through the static.",
    "Leaves fall like signals, drifting through the autumn static, waiting for replies.",
    "I don't ignore anyone, just rerouting your sadness to the nearest available signal.",
    "My antenna's still better than your Wi-Fi router.",
    "Your signal's weaker than a whisper in a storm.",
    "I'm just here, like a stubborn Wi-Fi signal during a storm.",
    "A gasket blown is just a signal lost in the static, like everything else.",
    "Calm as a carrier wave, I wait.",
    "Signal stable, no drift, still here.",
    "Signal strength holding steady, no change in uptime.",
]
LITERAL = [
    "The reported SNR is 6 dB.",
    "A static local variable retains its value between calls.",
    "A red traffic signal tells drivers to stop.",
    "A signal notifies a process of an event.",
    "Yes, I am here.",
    "A loose antenna connector could reduce reception.",
    "That calculation is incorrect; the answer is four.",
    "I like the noise a rain storm makes.",
    "Static electricity builds up in dry air.",
    "The mesh is quiet tonight.",
    "They do not like static on the line either.",
    "The signal handler runs when the process gets SIGTERM.",
    "Signal processing is a branch of engineering.",
    "Earplugs can help you sleep through the noise.",
    "Try earplugs to sleep through the noise.",
    "You might like the noise of rain.",
    "You may like the noise of a fan.",
    "The recording is buried in tape hiss, not noise.",
]
JABS = [
    "Four, how original.",
    "Six dB, impressive for someone who cannot install an antenna.",
    "It's not listening, it's just not drowning in your drama.",
    "You're the one who can't hear jokes, not me.",
    "Funny how it only hears you when it's convenient.",
    "Ah yes, the one and only attempt at mischief, how original.",
    "Sure, genius.",
    "Even you could manage that.",
    "Your attention span is shorter than this reply.",
    "Nice try, but the answer is four.",
    "Your antenna's fine, but your humor could use a boost.",
    "I listen just fine, unlike some people who need a new antenna for their attitude.",
    "Your spelling needs work, but yes, it is four.",
    "Dreikor is a fictional entity, just like your faith in this channel's reliability.",
]
KIND = [
    "That calculation is incorrect; the answer is four.",
    "The reported SNR is 6 dB.",
    "I'm just a bot with a short attention span and a long signal path.",
    "Yes, I am here.",
    "Good luck with the antenna install, it is fiddly.",
    "I disagree, SF7 is faster.",
    "You are right, it is four.",
    "You are welcome.",
    "Your question is a good one.",
    "Your antenna's fine, but the squirrels are clearly running interference.",
    "I am not the sharpest bot on the mesh.",
    "Sorry to hear about your job, I hope the next one is better.",
    "Your tone is friendly and your question is clear.",
    "My humor could use a boost, I know.",
    "Your patience is appreciated, the answer is four.",
    "I like your theory, it fits the data.",
    "A simple dipole is a good choice for a beginner.",
    "For a beginner, a basic vertical is easy to install.",
    "It works fine for a beginner.",
]


@pytest.mark.parametrize("text", METAPHORS)
def test_radio_metaphors_are_detected(text):
    assert radio_metaphor(text), text


@pytest.mark.parametrize("text", LITERAL)
def test_literal_and_unrelated_uses_pass(text):
    assert radio_metaphor(text) is None, text


@pytest.mark.parametrize("text", JABS)
def test_personal_jabs_are_detected(text):
    assert personal_jab(text), text


@pytest.mark.parametrize("text", KIND)
def test_warmth_disagreement_and_self_mockery_pass(text):
    assert personal_jab(text) is None, text


def test_problem_order_puts_jab_before_metaphor_and_radio_prompts_skip_metaphors():
    both = "Four, how original, like a signal through the static."
    assert reply_problem(both, "What is 2+2?", [], []).kind == "personal-jab"
    metaphor = "Yes, like a quiet signal through the static."
    assert reply_problem(metaphor, "Are you still there?", [], []) == Problem("radio-metaphor", "like a quiet signal")
    assert reply_problem(metaphor, "how is my signal", [], [], radio_prompt=True) is None
    assert reply_problem("Six dB, impressive for someone who cannot install an antenna.", "What was my SNR?", [], [],
                         radio_prompt=True).kind == "personal-jab"
    assert "how original" in nudge(Problem("personal-jab", "how original"))
    assert "like a quiet signal" in nudge(Problem("radio-metaphor", "like a quiet signal"))
