"""Decide, before any token is spent, whether a channel line wants an answer.

Only used when the trigger prefix is empty, that is, when the bot answers the
whole channel. A bare reaction (lol, an emoji, a thumbs up) has nothing to
answer, and a line that mentions someone with @[name] is part of a conversation
between people. Answering either costs airtime and reads as butting in. With
a trigger prefix the person addressed the bot on purpose and these do not apply.
"""

from __future__ import annotations

import re

_WORD_RE = re.compile(r"[^\W_]+(?:'[^\W_]+)*")  # letters in any script; emoji and punctuation are not words
_MENTION_RE = re.compile(r"@\[([^\]]*)\]")
_LAUGH_RE = re.compile(r"^(?:(?:ha|he){2,}h?|hi(?:hi)+h?|l+(?:o+l+)+z?|lm+f?ao+|rofl+|x+d+)$")

# Words that carry no question or request on their own. Yes and no are absent on
# purpose: they can answer something the bot asked.
REACTIONS = frozenset({
    "lol", "lmao", "lmfao", "rofl", "haha", "hehe", "heh", "ha",
    "k", "kk", "ok", "okay", "ty", "thx", "tnx", "tks", "thanks", "np", "yw",
    "yep", "yup", "nice", "cool", "wow", "omg", "wtf", "oof", "oh", "ah", "hmm", "hm", "meh",
    "sweet", "word", "same", "this", "brb", "gm", "gn", "gg", "rip", "lel", "kek", "ugh", "oops",
    "true", "facts", "fair", "noted", "indeed", "agreed", "welp", "eh", "huh", "yikes", "bruh",
})


def is_reaction(prompt: str) -> bool:
    """True for emoji or punctuation alone, or up to three reaction words. A question never is."""
    if "?" in prompt:
        return False
    text = re.sub(r"\bthank you\b", "thanks", prompt.lower())
    words = [w for w in _WORD_RE.findall(text) if w]
    if not words:
        return True
    if len(words) > 3:
        return False
    return all(w in REACTIONS or _LAUGH_RE.match(w) for w in words)


def mentions_someone(prompt: str, bot_name: str = "") -> bool:
    """A @[name] anywhere in the body, other than the bot's own name, means the line is for a person."""
    names = _MENTION_RE.findall(prompt)
    if not names:
        return "@[" in prompt  # an unclosed mention is still not for the bot
    return any(name.strip() != bot_name for name in names)
