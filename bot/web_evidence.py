"""Conservative text-support checks, not a proof of truth or semantic entailment."""
from __future__ import annotations

import re
from decimal import Decimal
from datetime import date, timedelta

from bot.reply import shape_reply

QUALIFIERS = (
    "rebate", "member", "summer", "winter", "seasonal", "holiday", "excluding", "except",
    "labor day", "memorial day", "chance", "likely", "clearance", "sale", "online only",
    "starting at", "per month", "refurbished", "used", "until", "expired", "in store only",
    "estimated", "as of", "before tax", "plus tax", "after tax", "shipping extra",
)


def normalized(text: str) -> str:
    return " ".join(text.lower().replace("-", " ").replace("–", " ").split())


def qualifiers(text: str) -> set[str]:
    text = normalized(text)
    return {word for word in QUALIFIERS if re.search(r"\b" + re.escape(word) + r"\b", text)}


_ONES = dict(zip("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen".split(), range(20)))
_TENS = dict(zip("twenty thirty forty fifty sixty seventy eighty ninety".split(), range(20, 100, 10)))
_NUMBER_WORDS = {**_ONES, **_TENS, "hundred": 100, "thousand": 1000}
_WORD_NUMBER = re.compile(r"\b(?:" + "|".join(_NUMBER_WORDS) + r")(?:[ -]+(?:and[ -]+)?(?:" + "|".join(_NUMBER_WORDS) + r"))*\b", re.I)


def numeric_text(text: str) -> str:
    def replace(match):
        total = group = 0
        for word in re.findall(r"[a-z]+", match[0].lower()):
            if word == "and":
                continue
            value = _NUMBER_WORDS[word]
            if value == 100:
                group = max(1, group) * 100
            elif value == 1000:
                total += max(1, group) * 1000
                group = 0
            else:
                group += value
        return str(total + group)
    return _WORD_NUMBER.sub(replace, text.lower()).replace(",", "")


_VALUE = r"\d+(?:\.\d+)?"


def money(text: str) -> set[tuple[str, Decimal]]:
    text = numeric_text(text)
    found = {(symbol, Decimal(value)) for symbol, value in re.findall(r"([$£€])\s*(" + _VALUE + r")", text)}
    units = {"dollars": "$", "dollar": "$", "usd": "$", "eur": "€", "euros": "€", "gbp": "£", "pounds": "£"}
    found.update((units[unit], Decimal(value)) for value, unit in re.findall(
        r"(" + _VALUE + r")\s*(dollars?|usd|eur|euros|gbp|pounds)\b", text))
    return found


def typed_numbers(text: str) -> set[tuple[str, str]]:
    text = numeric_text(text)
    units = {"percent": "%", "hours": "hour", "minutes": "minute", "days": "day", "lbs": "lb", "feet": "ft", "foot": "ft"}
    found = {(units.get(unit, unit), str(Decimal(value))) for value, unit in re.findall(
        r"(" + _VALUE + r")[ -]*(%|(?:percent|hours?|minutes?|days?|gb|tb|oz|lbs?|feet|foot|ft|inches|pack)\b)", text)}
    found.update(("clock", hour + ":" + (minute or "00") + meridiem) for hour, minute, meridiem in re.findall(
        r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", text))
    return found


_STOP = set("a an the is are was were be been being it its this that these those at in on of to from for with and or by as per has have had will would can could should do does did there here i we they you your their our my me us them about into than then also only according listed listing price priced cost costs before after at".split())
_ALIASES = {"opens": "open", "opening": "open", "closed": "close", "closing": "close", "hours": "hour", "sundays": "sunday", "forecasts": "forecast", "forecasted": "forecast", "raining": "rain", "snowing": "snow", "bulbs": "bulb", "boards": "board", "street": "st", "road": "rd", "avenue": "ave"}


def content_words(text: str) -> set[str]:
    # Currency spelling is checked by money(), so "$20" and "twenty dollars"
    # can agree without treating dollars as an unsupported content word.
    currency = {"dollar", "dollars", "usd", "eur", "euros", "gbp", "pounds"}
    return {_ALIASES.get(w, w) for w in re.findall(r"[a-z]+", numeric_text(text)) if w not in _STOP | currency}


def identity_words(prompt: str) -> set[str]:
    """Require named subjects and explicit place/store phrases in the source."""
    ignore = set("what where when which who how why current latest sunday monday tuesday wednesday thursday friday saturday january february march april may june july august september october november december weather forecast price hours cost library store opening is are does will tell please today tomorrow tonight the a an as of".split())
    names = {w.lower() for w in re.findall(r"\b[A-Z][A-Za-z0-9]+\b", prompt)} - ignore
    for match in re.finditer(r"\b(?:in|near|at|from)\s+([a-z][a-z ]*)", prompt, re.I):
        phrase = re.split(r"\b(?:today|tomorrow|tonight|this|on|for|as of|right now)\b", match[1], maxsplit=1, flags=re.I)[0]
        names.update(content_words(phrase) - ignore - {"stock", "sale", "least", "most", "home"})
    return {"wi" if name == "wisconsin" else name for name in names}


def supports(answer: str, quote: str, source: dict, sources: list[dict], prompt: str) -> bool:
    """Check both draft and transmitted text against scoped evidence and units."""
    full = source.get("full_text", source["text"])
    identities = identity_words(prompt)
    source_words = content_words(full.replace("Wisconsin", "WI").replace("wisconsin", "wi"))
    if not identities <= source_words:
        return False
    # Product identity may appear in a heading rather than the quoted price.
    identity_support = identities & source_words
    if money(quote):
        # A product heading may name the requested pack/variant, but arbitrary
        # words elsewhere on a page (including an injected answer object) are
        # not supporting evidence merely because the question used them too.
        heading = re.split(r"\n|\.\s", full, maxsplit=1)[0][:200]
        identity_support |= content_words(heading) & content_words(prompt)
    quote_words = content_words(quote)
    required = qualifiers(full) | set(source.get("qualifiers", []))
    for candidate in (answer, shape_reply(answer)):
        if not required <= qualifiers(candidate):
            return False
        negatives = {w for w in ("no", "not", "never", "without", "isn't", "aren't", "won't", "cannot", "can't")
                     if re.search(r"\b" + re.escape(w) + r"\b", quote, re.I)}
        if any(not re.search(r"\b" + re.escape(w) + r"\b", candidate, re.I) for w in negatives):
            return False
        words = content_words(candidate)
        # Every content word must be present in the supporting passage or in a
        # requested product/place identity independently present on this page.
        if not words <= quote_words | identity_support | {"forecast", "listed"}:
            return False
        if not money(candidate) <= money(quote):
            return False
        if not typed_numbers(candidate) <= typed_numbers(quote) | (typed_numbers(prompt) & typed_numbers(full)):
            return False
        nums = lambda s: set(re.findall(_VALUE, numeric_text(s)))
        identity_numbers = set(re.findall(r"(" + _VALUE + r")[ -]*(?:pack|gb|tb|oz|lb|foot|ft|inch)", numeric_text(full)))
        if not nums(candidate) <= nums(quote) | identity_numbers:
            return False
    price = bool(money(answer))
    weather = bool(re.search(r"\b(?:weather|forecast|rain|snow)\b", prompt, re.I))
    relevant = [s for s in sources if identities <= content_words(
        s.get("full_text", s["text"]).replace("Wisconsin", "WI").replace("wisconsin", "wi"))]
    if price:
        if len(money(quote)) != 1:
            return False
        if not re.search(r"\blisted\b", answer, re.I):
            return False
        # Conflicting displayed amounts require an abstention; selecting the
        # smallest/oldest amount is not evidence of the requested current price.
        amounts = set().union(*(money(s.get("full_text", s["text"])) for s in relevant))
        if amounts - money(quote):
            return False
    if weather:
        try:
            requested_day = date.fromisoformat(source["as_of"]) + timedelta(days=bool(re.search(r"\btomorrow\b", prompt, re.I)))
        except (KeyError, ValueError):
            return False
        explicit_dates = re.findall(r"\b\d{4}-\d{2}-\d{2}\b", full)
        month_dates = re.findall(r"\b" + requested_day.strftime("%B") + r"\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?\b", full, re.I)
        if (requested_day.isoformat() not in explicit_dates and not any(
                int(day) == requested_day.day and (not year or int(year) == requested_day.year)
                for day, year in month_dates)):
            return False
        if not re.search(r"\b(?:forecast|chance|likely|possible|may|expected)\b", answer, re.I):
            return False
        if re.search(r"\b(?:will|definitely|certainly|guaranteed)\b", answer, re.I):
            return False
        if re.search(r"\b(?:rain|snow)\b", answer, re.I) and any(re.search(
            r"\b(?:0\s*(?:%|percent)|no (?:rain|snow|precipitation))", numeric_text(s.get("full_text", s["text"]))) for s in relevant):
            return False
    return True
