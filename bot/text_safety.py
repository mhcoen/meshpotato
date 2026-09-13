"""Boundaries for unauthenticated channel text, independent of model heuristics."""

import re
import unicodedata

_BLOCK_MARKER = re.compile(r"<<<\s*(?:BEGIN|END)\b", re.I)


def has_block_marker(text: str) -> bool:
    return bool(_BLOCK_MARKER.search(text))


def safe_sender(sender: str) -> bool:
    return bool(sender) and "]" not in sender and "@[" not in sender and not any(
        unicodedata.category(c) in {"Cc", "Cs", "Zl", "Zp"} for c in sender
    ) and not has_block_marker(sender)


def transcript_field(text: str) -> str:
    """A stored field cannot introduce another row or close a context block."""
    return " ".join(text.split()).replace("<<<", "< < <").replace(">>>", "> > >")


def forged_frame(body: str, bot_name: str) -> bool:
    return has_block_marker(body) or bool(re.search(
        rf"(?:^|[\r\n\u2028\u2029])\s*{re.escape(bot_name)}:", body, re.I
    ))
