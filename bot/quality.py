"""Outbound reply checks: no repeats, no parroting, no mentions, no radio metaphors
on non-radio questions, no jokes at the person asking.

Small models copy their own earlier replies out of the context blocks, echo the
message they were sent, reach for the same signal-through-the-static image in
every joke, and aim jokes at the asker, and all of it survives every instruction
against it. These checks run on the shaped reply, before it costs airtime. The
caller gives the model one more try with a pointed nudge, then stays silent.

The two content checks are deliberately structural rather than word lists. A radio
metaphor needs a simile or comparison whose object is a radio noun, a stock
figurative frame around "static" or "noise", or a radio verb applied to a feeling.
A personal jab needs a sarcastic tag, a competence clause about the asker, an
insulting second-person predicate, a pejorative possessive, or a belittling
rhetorical frame. What they cannot see: a bare figurative statement with no
marker ("the signal fades, and I am still here"), sarcasm carried by tone alone
("congratulations, you have broken the streak"), a comparison to the asker
outside the one covered frame ("just like your faith in ..." is caught, most
others are not), or a metaphor in a reply to a question that mentions radio,
where radio imagery is on topic.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass

PASS_WORD = "PASS"

# How alike two replies must be to count as the same reply. Replies to the same
# person repeat in form more easily, so the bar is lower there. Very short replies
# ("Yes.", "Good morning.") recur for good reasons and carry no signature phrasing.
SAME_SENDER_RATIO = 0.75
OTHER_SENDER_RATIO = 0.9
MIN_REPEAT_CHARS = 10
# Below this length only an exact match counts: "The repeater is online." after "The
# repeater is offline." is a different answer, however alike the letters.
EXACT_ONLY_BELOW = 40

_NORMALIZE_RE = re.compile(r"[^a-z0-9]+")
# Politeness in front of a question: "Please explain SF7", "hey, what time is it".
_POLITE_RE = re.compile(
    r"^(?:(?:please|hey|hi|hello|ok|okay|yo|so|well|um|uh|dear|sorry|excuse me|quick question)[,:!.\s]+)+", re.I
)
_VOCATIVES = ("mesh potato", "potato", "bot")
_PUNCT_RE = re.compile(r"^[,:!.]\s*")
# After a vocative without punctuation ("hey potato what time is it") only these count as
# openers; "Mesh Potato is broken again" is a statement about the bot, not a question.
_WH_RE = re.compile(
    r"^(?:what|what's|whats|where|where's|when|who|who's|whom|whose|why|how|how's|which|tell|give|show|explain|"
    r"describe|list|name|recommend|suggest|remind|help)\b",
    re.I,
)
_ASKS_RE = re.compile(
    r"^(?:what|what's|whats|where|where's|when|who|who's|whom|whose|why|how|how's|which|is|are|am|was|were|can|could|"
    r"do|does|did|will|would|should|shall|may|might|have|has|had|tell|give|show|explain|describe|list|name|"
    r"recommend|suggest|remind|help|any|anyone|anybody)\b",
    re.I,
)
_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")


@dataclass(frozen=True)
class Problem:
    kind: str  # "mention", "parrot", "repeat", "personal-jab", "radio-metaphor", or "pass"
    text: str = ""  # the earlier reply that was repeated, or the matched phrase, for the log and the nudge


# ---- radio metaphors ---------------------------------------------------------------
_RADIO_NOUN = (r"(?:signals?|static|noise|antennas?|wi-?fi|packets?|repeaters?|reception|frequenc(?:y|ies)|bandwidth|"
               r"airtime|transmissions?|radio(?: waves?)?|carrier|rssi|snr|lora|mesh)")
_DET = r"(?:(?:a|an|the|your|my|our|their|his|her|its|some|any|every|this|that|one)\s+)?"
# "like" after a subject pronoun is the verb ("I like the noise"), not a simile.
_NOT_VERB_LIKE = (r"(?<!\bi )(?<!\bwe )(?<!\byou )(?<!\bthey )(?<!\bwho )(?<!\bpeople )(?<!'d )(?<!n't )(?<!\bnot )(?<!\bdo )(?<!\bdid )"
                  r"(?<!\bmight )(?<!\bmay )(?<!\bwould )(?<!\bcould )(?<!\bwill )(?<!\bshould )(?<!\bcan )(?<!\balso )(?<!\breally )(?<!\bprobably )")
_SIMILE_RE = re.compile(
    rf"{_NOT_VERB_LIKE}\b(?:like|unlike|as if|as though)\s+{_DET}(?:\w+\s+){{0,3}}{_RADIO_NOUN}\b"  # like a quiet signal
    rf"|\bas\s+\w+\s+as\s+{_DET}(?:\w+\s+){{0,2}}{_RADIO_NOUN}\b"  # as faint as a signal
    rf"|\b(?:calm|quiet|steady|faint|weak|strong|clear|lost|silent|patient|loud|sharp|stubborn|constant|fickle)\s+as\s+"
    rf"{_DET}(?:\w+\s+){{0,2}}{_RADIO_NOUN}\b"  # calm as a carrier wave; "works as a repeater" is a role, not a simile
    rf"|\b{_RADIO_NOUN}(?:'s|\s+(?:is|are|was|were))?\s+(?:\w+\s+){{0,2}}"
    r"(?:weaker|stronger|clearer|fainter|louder|quieter|better|worse)\s+than\b"  # your signal's weaker than
    rf"|\bthan\s+{_DET}(?:\w+\s+){{0,2}}{_RADIO_NOUN}\b",  # better than your Wi-Fi
    re.I,
)
# "through the noise" alone is how people describe sleeping past the neighbours; the frame needs
# a figurative verb in front of it.
_FIGURATIVE_RE = re.compile(
    r"\b(?:lost in|drifting (?:through|in|into)|fading (?:into|in)|buried in|swallowed by|adrift in|dissolving into|"
    r"whisper(?:s|ing)? (?:in|through)|a (?:whisper|voice|spark|ghost) in)\s+(?:the\s+|your\s+|all the\s+|this\s+)?(?:\w+\s+){0,2}(?:static|noise)\b",
    re.I,
)
_FEELING = (r"(?:sadness|feelings|drama|mood|ego|attitude|hopes?|dreams?|worries|sarcasm|enthusiasm|patience|opinions?|"
            r"complaints?|nonsense|whining|thoughts|anxiety|excitement|frustration)")
_RADIO_VERB_RE = re.compile(
    rf"\b(?:rerout(?:e|ed|ing)|transmit(?:ted|ting)?|broadcast(?:ed|ing)?|beam(?:ed|ing)?|relay(?:ed|ing)?|"
    rf"tun(?:e|ed|ing) (?:in|out)|jam(?:med|ming)?)\s+(?:your|my|the|our|his|her|their)\s+(?:\w+\s+)?{_FEELING}\b",
    re.I,
)


# A reply that opens as a radio status report ("Signal stable, no drift") when nobody asked
# about radio is the old signal-report habit in a new coat.
_STATUS_OPENER_RE = re.compile(
    r"^(?:signal|signals|rssi|snr|reception|carrier|link)\b[^.!?,]{0,20}\b(?:holding|steady|stable|strong|weak|clear|fine|good|ok|solid|nominal)\b",
    re.I,
)


def radio_metaphor(body: str) -> str | None:
    """The figurative radio phrase in ``body``, or None. Literal uses carry no marker."""
    for pattern in (_SIMILE_RE, _FIGURATIVE_RE, _RADIO_VERB_RE, _STATUS_OPENER_RE):
        m = pattern.search(body)
        if m:
            return m.group(0)
    return None


# ---- personal jabs -----------------------------------------------------------------
_SARCASTIC_TAG_RE = re.compile(
    r"\bhow (?:original|creative|clever|novel|profound|insightful|ambitious|brave|impressive|thoughtful|kind of you)\b"
    r"|\bbold of you\b|\bnice try\b|\bbless your heart\b|\bgood luck with that\b|\bwhat a (?:genius|surprise|shock)\b",
    re.I,
)
_SARCASTIC_VOCATIVE_RE = re.compile(r",\s*(?:genius|einstein|sherlock|professor|champ|sport|smart ?guy|big ?shot)\s*[.!?]*$", re.I)
_FOR_SOMEONE_RE = re.compile(
    r"\bfor (?:someone|somebody|a person|anyone|a (?:guy|man|woman|beginner|newbie|noob|first[- ]timer)) who "
    r"(?:can(?:'t|not)|couldn't|could not|does ?n(?:'|o)t|did ?n(?:'|o)t|has ?n(?:'|o)t|barely|never|still|just|only|clearly|obviously)\b",
    re.I,
)  # "for a beginner" alone describes suitable equipment, not the asker
_YOU_ARE_RE = re.compile(
    r"\byou(?:'re| are)\s+(?:clearly |obviously |apparently |evidently |just |such |still )?(?:an? )?"
    r"(?:idiot|moron|fool|clueless|hopeless|dense|dumb|stupid|slow|lazy|illiterate|incompetent|delusional|pathetic|useless|"
    r"a joke|not the (?:sharpest|brightest)|too (?:dumb|slow|lazy|dense|thick)|the one who (?:can'?t|cannot|couldn't|doesn'?t|won'?t|never))\b",
    re.I,
)
_YOUR_PEJORATIVE_RE = re.compile(
    r"\byour (?:\w+ )?(?:drama|whining|complaining|nagging|excuses|tantrum|meltdown|ego|ignorance|incompetence|stupidity|"
    r"delusions?|paranoia|nonsense|babbling|rambling)\b",
    re.I,
)
_YOUR_FACULTY_RE = re.compile(
    r"\byour (?:\w+ )?(?:iq|intelligence|brain|attention span|reading comprehension|competence|understanding|grasp)\b"
    r"[^.!?]{0,25}\b(?:is|are|seems?|remains?|was|were) (?:\w+ )?(?:lacking|limited|questionable|missing|absent|nonexistent|weak|short|small|tiny|low|below|elsewhere|"
    r"shorter|smaller|lower|weaker|thinner|worse|slower)\b"
    r"|\b(?:lack|lacking|short) of (?:\w+ )?(?:brains?|intelligence|competence|sense)\b",
    re.I,
)
# "your humor could use a boost": a trait of the asker plus a verdict that it is deficient.
_YOUR_TRAIT_RE = re.compile(
    r"\byour (?:\w+ )?(?:humor|humour|jokes?|tone|attitude|manners|spelling|grammar|logic|math|aim|timing|taste|memory|patience|"
    r"reading|listening|typing|comprehension)\b[^.!?]{0,12}\b(?:could use|needs?|wants?|is|are|was|were) (?:a |some |clearly |still )?"
    r"(?:boost|work|help|improvement|upgrade|tuning|lacking|off|rusty|questionable|missing|worse|weak)\b",
    re.I,
)
_RHETORICAL_RE = re.compile(
    r"\beven you\b|\bunlike (?:you|some people|certain people|some folks|others here)\b"
    r"|" + _NOT_VERB_LIKE + r"\b(?:just )?like your (?:faith|trust|belief|beliefs|confidence|hopes?|chances|odds|track record|credibility|reputation|theory|theories)\b"
    r"|\bif you (?:even |ever )?(?:knew|understood|could read|had (?:read|listened)|bothered)\b"
    r"|\byou (?:finally|actually) (?:managed|figured|noticed|got it|read)\b|\bfunny how\b[^.!?]{0,40}\byou\b",
    re.I,
)


def personal_jab(body: str) -> str | None:
    """The belittling phrase in ``body``, or None. Warmth, disagreement and self-mockery carry none of these."""
    for pattern in (_SARCASTIC_TAG_RE, _SARCASTIC_VOCATIVE_RE, _FOR_SOMEONE_RE, _YOU_ARE_RE, _YOUR_PEJORATIVE_RE,
                    _YOUR_FACULTY_RE, _YOUR_TRAIT_RE, _RHETORICAL_RE):
        m = pattern.search(body)
        if m:
            return m.group(0)
    return None


def third_party_jab(body: str) -> str | None:
    """Refuse direct defamatory/insulting predicates even about someone else."""
    match = re.search(
        r"\b(?:[a-z][a-z'-]*\s+){1,4}(?:is|are|was|were)\s+"
        r"(?:(?:a|an|the|total|complete|lying|real)\s+)*"
        r"(?:fraud|liar|idiot|scammer|criminal|thief|thieves|moron|crook)\b", body, re.I)
    return match[0] if match else None


def normalize(text: str) -> str:
    return " ".join(_NORMALIZE_RE.sub(" ", text.lower()).split())


def is_pass(body: str) -> bool:
    return normalize(body) == PASS_WORD.lower()


def looks_like_question(prompt: str, bot_name: str = "") -> bool:
    """A question mark, or an interrogative or request opener; people on a phone skip the mark."""
    if "?" in prompt:
        return True
    text = _POLITE_RE.sub("", prompt.strip())
    if _ASKS_RE.match(text):
        return True
    names = sorted(set(_VOCATIVES) | ({bot_name.lower()} if bot_name else set()), key=len, reverse=True)
    for name in names:
        if not text.lower().startswith(name) or len(text) == len(name):
            continue
        rest = text[len(name):]
        if _PUNCT_RE.match(rest):
            return bool(_ASKS_RE.match(_POLITE_RE.sub("", _PUNCT_RE.sub("", rest))))
        if rest[0].isspace():
            return bool(_WH_RE.match(rest.lstrip()))
    return False


def has_mention(body: str) -> bool:
    return "@[" in body


def is_parrot(body: str, prompt: str) -> bool:
    """The reply is the message, a fragment of it, or the message with a tail.

    A fragment is a fine answer to a question that offers choices ("915 MHz." to
    "868 or 915 MHz?"), so the fragment rule applies only to non-questions.
    """
    b, p = normalize(body), normalize(prompt)
    if not b or not p:
        return False
    if len(p.split()) < 2:
        return False  # "Hello?" answered with "Hello." is how people talk
    if b == p:
        return True
    if len(b) >= 4 and b in p and (len(b.split()) > 3 or not looks_like_question(prompt)):
        return True  # a short fragment may answer a choice question; a sentence lifted out of it is a relay
    if len(p.split()) >= 3 and b.startswith(p):
        return True
    # a paraphrased echo of a remark ("At least it listens when it matters" for "at least it listens to me
    # when it matters lol"); a question is exempt, because "Yes, serious mode resets after 120 minutes" is
    # supposed to overlap "Does serious mode reset after 120 minutes?"
    return len(b) >= 15 and not looks_like_question(prompt) and difflib.SequenceMatcher(None, b, p).ratio() >= 0.85


def find_repeat(body: str, same_sender: list[str], other_senders: list[str]) -> str | None:
    """The earlier reply this one duplicates, or None.

    Two replies with different numbers in them are different answers (two people's
    signal reports, two dice rolls), however alike the words around them.
    """
    b = normalize(body)
    if len(b) < MIN_REPEAT_CHARS:
        return None
    numbers = _NUMBER_RE.findall(body)
    for earlier, bar in [(e, SAME_SENDER_RATIO) for e in same_sender] + [(e, OTHER_SENDER_RATIO) for e in other_senders]:
        e = normalize(earlier)
        if not e or _NUMBER_RE.findall(earlier) != numbers:
            continue
        if e == b:
            return earlier
        if len(b) < EXACT_ONLY_BELOW:
            continue
        matcher = difflib.SequenceMatcher(None, b, e)
        if matcher.real_quick_ratio() >= bar and matcher.quick_ratio() >= bar and matcher.ratio() >= bar:
            return earlier
    return None


def reply_problem(body: str, prompt: str, same_sender: list[str], other_senders: list[str], *,
                  radio_prompt: bool = False) -> Problem | None:
    """The first problem found, worst first. ``radio_prompt`` says the question itself is about radio,
    where radio imagery is on topic and the metaphor check does not apply."""
    if has_mention(body):
        return Problem("mention")
    if is_parrot(body, prompt):
        return Problem("parrot")
    earlier = find_repeat(body, same_sender, other_senders)
    if earlier is not None:
        return Problem("repeat", earlier)
    jab = personal_jab(body) or third_party_jab(body)
    if jab is not None:
        return Problem("personal-jab", jab)
    if not radio_prompt:
        metaphor = radio_metaphor(body)
        if metaphor is not None:
            return Problem("radio-metaphor", metaphor)
    return None


def nudge(problem: Problem) -> str:
    """The extra user turn for the one retry. It never offers PASS: given the exit, the model takes it."""
    if problem.kind == "mention":
        return "Your reply addressed or mentioned someone with @[. Never do that. Answer the message again without it."
    if problem.kind == "parrot":
        return ("Your reply repeated the message you were sent instead of answering it. "
                "Reply again in your own words, without quoting the message.")
    if problem.kind == "pass":
        return (f"The message is a question or request to you, so {PASS_WORD} is not allowed. Answer it; "
                "if you cannot, say so in a few words.")
    if problem.kind == "personal-jab":
        return (f"Your reply insulted a person ({problem.text}). Keep the voice, but aim any joke at the "
                "question, the weather, or yourself, never at anyone, and answer again.")
    if problem.kind == "radio-metaphor":
        return (f"Your reply used radio imagery ({problem.text}) and the message is not about radio. Answer it again "
                "with no radio, signal, static, antenna or Wi-Fi imagery at all.")
    return (f"You already sent this reply a moment ago: {problem.text} "
            "Do not send it again and do not reword it. Answer the current message with a different, "
            "specific reply.")
