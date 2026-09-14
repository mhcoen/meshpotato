"""Sports intents and bounded follow-up wording; no model or network routing."""
import re
from bot.sports_names import TEAM_NAMES

# These nicknames also name ordinary things. Lowercase use needs sports context.
COMMON_NAMES = frozenset('sun sky heat wild fire bulls rays kings jazz wings reds twins magic thunder lightning stars sparks dream liberty fever storm mercury aces nets pelicans hornets hawks rockets warriors giants rangers guardians athletics royals angels tigers pirates cardinals orioles blue jays saints jets bears lions panthers falcons eagles ravens commanders titans chargers colts browns bills dolphins ducks senators predators avalanche islanders devils kraken capitals'.split())
NONSPORT = re.compile(r"\b(?:credit|exam|school|scrabble|video game|define|meaning|musical|resume|poem|band|concert|lawsuit|court|package|parcel|market|stocks|radio|antenna|bar|restaurant)\b|\bhow (?:do|does|to)\b.*\b(?:work|calculate|score)\b", re.I)
SCOPE = re.compile(r"\b(?:nfl|nba|wnba|mlb|nhl|football|basketball|baseball|hockey|game|match|standings|division|conference)\b", re.I)


def normalized(query):
    return " ".join(re.findall(r"[a-z0-9]+", query.lower()))


def has_team(query):
    text = " " + normalized(query) + " "
    for name in TEAM_NAMES:
        if " " + normalized(name) + " " not in text:
            continue
        if name not in COMMON_NAMES or SCOPE.search(query):
            return True
        if any(m.group()[0].isupper() for m in re.finditer(r"\b" + re.escape(name) + r"\b", query, re.I)):
            return True
    return False


def sports_kind(query, *, has_context=False):
    from bot.sports import is_score_query
    if NONSPORT.search(query):
        return None
    q = query.lower().replace("’", "'")
    team = has_team(query)
    hint = team or bool(re.search(r"\b(?:nfl|nba|wnba|mlb|nhl)\b", q)) or (has_context and bool(re.search(r"\b(?:they|their|them)\b", q)))
    if re.search(r"\bgames? (?:behind|back)\b", q) or (hint and re.search(r"\bhow far (?:behind|back|ahead)\b", q)):
        return "behind"
    if hint and re.search(r"\b(?:first|last|\d+(?:st|nd|rd|th)) place\b", q):
        return "standings"
    if re.search(r"\b(?:lead(?:ing|s)?|first|atop|top)\b", q) and re.search(r"\b(?:division|conference|nl|al|nfc|afc|(?:national|american) league)\b", q):
        return "leader"
    if hint and re.search(r"\brecord\b", q):
        return "standings"
    if re.search(r"\bstandings\b", q) or (hint and re.search(r"\bstanding\b|\b(?:what|which) (?:place|position)|\brank(?:ed|ing)?\b|\bwhere\b.*\bstand\b", q)):
        return "standings"
    if hint and (re.search(r"\b(?:next (?:game|match)|play(?:ing)? next)\b", q)
                 or re.search(r"\bwhen\b.*\bplay\b|\b(?:when|what time)\b.*\b(?:game|match)\b", q)):
        return "next"
    if hint and re.search(r"\bhow\b.*\bdoing\b", q):
        return "score" if re.search(r"\b(?:game|tonight|right now)\b", q) else "overview"
    if is_score_query(q) or (hint and re.search(r"\b(?:who won|did .+ win|winning|losing|result)\b", q)):
        return "score"
    return None


def is_sports_query(query):
    return sports_kind(query) is not None


def with_team_context(query, context):
    if (context and sports_kind(query, has_context=True) and not has_team(query)
            and not re.search(r"\b(?:nl|al|nfc|afc|nfl|nba|wnba|mlb|nhl)\b", query, re.I)):
        if re.search(r"\b(?:they|their|them|division)\b", query, re.I):
            return f"{query} ({context['team']}, {context['league'].upper()})"
    return query
