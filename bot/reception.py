"""Bounded reception facts from this delivered event, never global last-packet stats."""

import re
from typing import Any

# Only a question about reception gets the reception block. Given the numbers on
# every message, small models recite them in reply to greetings and remarks.
_RECEPTION_RE = re.compile(
    r"\b(?:rssi|snr|dbm|signal report|signal strength|(?:my|the) signal|reception|receiv\w*|"
    r"(?:how many|how much|no|zero|\d+|my|the) hops?|hop count|copy|copies|(?:get|got|come|came) through|loud and clear|"
    r"my (?:message|messages|packet|packets|msg|transmission)|hear(?:d|ing)? (?:me|us)|reach(?:ed)? you|"
    r"directly|direct (?:to you|reception|or (?:via|through|over))|"
    r"(?:did|do|can|could) (?:you|u|anyone|anybody) (?:get|hear|catch|read|see) (?:me|this|that|it|my))\b",
    re.I,
)

# Appended to the persona for a reception question. In the funny voice the joke
# otherwise wins over the numbers, and a direct message was once described as
# having hopped through repeaters.
RECEPTION_VOICE = (
    "If the message asks how it was received or how strong it was, state the RSSI, SNR and hop count from "
    "the reception block plainly first, treat a hop count of 0 as a direct reception with no repeater, and "
    "keep the rest short; if it asks about something else, answer that instead."
)


def asks_about_reception(prompt: str) -> bool:
    return bool(_RECEPTION_RE.search(prompt))


_COUNT_WORDS = dict(zip("zero one two three four five six seven eight nine ten".split(), range(11)))
_REPORT_WORDS = set("rssi snr dbm db hop hops count this your the message packet reports reported was is "
                    "received at with and in by bot me copies may differ unavailable unknown".split()) | set(_COUNT_WORDS)
_VALUE = r"(?:-?\d+(?:\.\d+)?|zero|one|two|three|four|five|six|seven|eight|nine|ten|unavailable|unknown)"
_RADIO_VALUE = re.compile(rf"\b(rssi|snr)\s*(?:in\s+dbm?\s*)?(?::|=|is|was)?\s*({_VALUE})(?=\s|dbm?\b|[,.;)]|$)", re.I)
_HOP_VALUE = re.compile(rf"\b(?:hop(?:s| count)?\s*(?::|=|is|was)?\s*({_VALUE})\b|({_VALUE})\s+hops?\b)", re.I)


def is_plain_reception_report(text: str, context: str) -> bool:
    """Conservative repeat exception for plain readings matching this question.

    A reception question alone does not make an old joke safe to repeat. Only
    a small report vocabulary and explicitly labelled current values qualify;
    richer prose follows the ordinary repeat/retry path.
    """
    if not set(re.findall(r"[a-z]+", text.lower())) <= _REPORT_WORDS:
        return False

    def value(raw: str):
        raw = raw.lower()
        if raw in {"unknown", "unavailable"}:
            return None
        return _COUNT_WORDS[raw] if raw in _COUNT_WORDS else float(raw)

    def readings(raw: str):
        found = [(name.lower(), value(v)) for name, v in _RADIO_VALUE.findall(raw)]
        found.extend(("hops", value(a or b)) for a, b in _HOP_VALUE.findall(raw))
        return found

    expected = dict(readings(context))
    reported = readings(text)
    if not reported or any(name not in expected or expected[name] != v for name, v in reported):
        return False
    # Reject additional, unlabelled numbers that have no matching measurement.
    numbers = {float(n) for n in re.findall(r"-?\d+(?:\.\d+)?", text)}
    return numbers <= {v for _, v in reported if v is not None}


def _measurement(payload: dict[str, Any], key: str, low: float, high: float) -> str:
    # MeshCore exposes uppercase RSSI/SNR on CHANNEL_MSG_RECV, unlike RX logs.
    value = payload.get(key)
    if type(value) not in (int, float) or not low <= value <= high:
        return "unavailable"
    return f"{value:g}"


def reception_context(payload: dict[str, Any]) -> str:
    """Snapshot validated scalar fields before queueing; no per-sender cache.

    Firmware V3 supplies SNR; the library attaches RSSI from the latest matching
    heard copy, which may have followed a different route. Older frames get both
    RSSI and SNR from that log. The event does not explicitly identify its version,
    so do not infer provenance from field presence or matching path lengths.
    The path-length sentinel 255 does not mean 255 hops.
    """
    hops = payload.get("path_len")
    hop_text = str(hops) if type(hops) is int and 0 <= hops <= 63 else "unavailable"
    return (
        "Current question at this bot, copies may differ:\n"
        f"RSSI in dBm: {_measurement(payload, 'RSSI', -128, 127)} "
        "(latest library-matched heard copy); "
        f"SNR in dB: {_measurement(payload, 'SNR', -32, 31.75)} "
        "(delivered copy on V3, matched heard copy on older frames); "
        f"reported hop count: {hop_text} (delivered copy).\n"
        "Not a verified single reception, even if path lengths match; not every hop. "
        "Repeater identities and measurements of earlier messages are unavailable."
    )
