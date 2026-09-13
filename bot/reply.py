"""Shape the model's output into a single channel-safe line and enforce the cap.

Answer text is plain ASCII with ordinary punctuation. Dashes become
commas, ellipses become periods, curly quotes become straight ones, accented letters
lose their accents, and anything else outside ASCII (emoji, symbols) is dropped. One
byte per answer character on the air. Sender mentions preserve the original Unicode
name; their UTF-8 byte cost is accounted for separately.
"""

from __future__ import annotations

import re
import unicodedata

from bot.text_safety import safe_sender

_THINK_RE = re.compile(r"<think>.*?(?:</think>|$)", re.DOTALL | re.IGNORECASE)
_SENTENCE_RE = re.compile(r'''[.!?]["']?(?=\s|$)''')
# Titles, dotted initialisms, and a compass letter after a number ("1200 N. Stoughton
# Rd") never end a sentence; a lone letter otherwise does ("plan B. Then relax.").
_ABBREVIATION_RE = re.compile(
    r"(?:\b(?:dr|mr|mrs|ms|prof|sr|jr|st|vs|etc|sgt|capt|lt|col|gen|rev|hon)|\b[a-z](?:\.[a-z])+|\d\s+[nsew])\.$",
    re.I,
)
# Street and unit abbreviations can end a sentence ("25 mph. Drive safely."); they
# continue it only when a lowercase word follows ("Park Ave. near the lake").
_UNIT_ABBREVIATION_RE = re.compile(
    r"\b(?:ave|blvd|hwy|rd|ct|ln|mt|ft|co|inc|ltd|approx|dept|univ|bros|fig|vol|mph|kph|kmh|lbs|oz)\.$", re.I
)
_QUOTE_PAIRS = (('"', '"'), ("'", "'"), ("\u201c", "\u201d"), ("\u2018", "\u2019"))

# Dashes used as separators read as commas; a hyphen inside a word stays a hyphen.
_DASH_RE = re.compile(r"\s*[\u2014\u2013\u2015]\s*|\s+-\s+")
_ELLIPSIS_RE = re.compile(r"\u2026|\.{3,}")
_QUOTE_MAP = str.maketrans({"\u201c": '"', "\u201d": '"', "\u2018": "'", "\u2019": "'", "\u00b4": "'", "\u2032": "'"})
_SPACE_BEFORE_PUNCT_RE = re.compile(r"\s+([,.!?;:])")
_DOUBLE_COMMA_RE = re.compile(r",(\s*,)+")
_COMMA_BEFORE_STOP_RE = re.compile(r",\s*([.!?])")


def strip_think(text: str) -> str:
    """Remove any <think>...</think> block a reasoning model may have leaked."""
    return _THINK_RE.sub("", text)


def collapse_whitespace(text: str) -> str:
    """Collapse all runs of whitespace, including newlines, to single spaces."""
    return " ".join(text.split())


def strip_wrapping_quotes(text: str) -> str:
    for left, right in _QUOTE_PAIRS:
        if len(text) >= 2 and text.startswith(left) and text.endswith(right):
            return text[1:-1].strip()
    return text


def plain_ascii(text: str) -> str:
    """Reduce to ASCII with ordinary punctuation. See the module docstring."""
    text = text.translate(_QUOTE_MAP)
    text = _DASH_RE.sub(", ", text)
    text = _ELLIPSIS_RE.sub(".", text)
    text = text.replace(";", ",")
    text = unicodedata.normalize("NFKD", text)
    text = text.replace("\u2044", "/").replace("\u2212", "-")
    text = text.encode("ascii", "ignore").decode("ascii")
    text = collapse_whitespace(text)
    text = "".join(c for c in text if " " <= c <= "~")
    text = _SPACE_BEFORE_PUNCT_RE.sub(r"\1", text)
    text = _DOUBLE_COMMA_RE.sub(",", text)
    text = _COMMA_BEFORE_STOP_RE.sub(r"\1", text)
    return text.strip(" ,")


def first_sentence(text: str) -> str:
    """Keep only the first sentence, plus the next one if the first is a question.

    A terminator followed by a non-space (3.5, e.g.) does not split. A question on its
    own is never a complete reply (riddle-style jokes), so its answer is kept too.
    """
    keep_answer = False
    for match in _SENTENCE_RE.finditer(text):
        if match.group().startswith("."):
            head = text[:match.start() + 1]
            tail = text[match.end():].lstrip()
            if _ABBREVIATION_RE.search(head):
                continue
            if _UNIT_ABBREVIATION_RE.search(head) and tail and not tail[0].isupper():
                continue
        if not keep_answer and match.group().startswith("?"):
            keep_answer = True
            continue
        return text[:match.end()]
    return text


def shape_reply(raw: str) -> str:
    """The full outbound normalisation: strip think blocks, collapse, unquote, ASCII, first sentence."""
    text = collapse_whitespace(strip_think(raw))
    text = plain_ascii(strip_wrapping_quotes(text))
    return first_sentence(text).strip()


def reply_prefix(sender: str) -> str:
    return f"@[{sender}] "


def reply_body_room(sender: str, max_chars: int, max_bytes: int) -> int:
    """Room for an ASCII body after an exact-name mention, under both caps."""
    prefix = reply_prefix(sender)
    # Preserve display names, including emoji joiners, but never emit line breaks,
    # terminal controls, or invalid UTF-8. Reject rather than change the identity.
    if not safe_sender(sender):
        return 0
    return min(max_chars - len(prefix), max_bytes - len(prefix.encode("utf-8")))


def compose_reply(sender: str, text: str, max_chars: int, *, max_bytes: int = 160) -> str | None:
    """Compose a complete reply, or return None; never slice a body or prefix."""
    prefix = reply_prefix(sender)
    available = reply_body_room(sender, max_chars, max_bytes)
    body = text.strip()
    if available <= 0 or not body or len(body) > available:
        return None
    reply = prefix + body
    if len(reply.encode("utf-8")) > max_bytes:
        return None
    assert len(reply) <= max_chars
    return reply
