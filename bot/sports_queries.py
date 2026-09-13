"""Sports intents and bounded follow-up wording; no model or network routing."""
import re
from bot.sports_names import TEAM_NAMES


def normalized(query):
    return " ".join(re.findall(r"[a-z0-9]+", query.lower()))


def has_team(query):
    text = " " + normalized(query) + " "
    return any(" " + normalized(name) + " " in text for name in TEAM_NAMES)


def sports_kind(query):
    from bot.sports import is_score_query
    q = query.lower().replace("’", "'")
    if re.search(r"\b(?:credit|exam|scrabble|video game|define|meaning|musical|resume|poem|band|concert)\b|\bhow (?:do|does|to)\b.*\b(?:work|calculate|score)\b", q):
        return None
    hint = has_team(q) or bool(re.search(r"\b(?:nfl|nba|wnba|mlb|nhl|they|their|them)\b", q))
    if re.search(r"\b(?:games? (?:behind|back)|how far (?:behind|back|ahead))\b", q):
        return "behind"
    if re.search(r"\b(?:lead(?:ing|s)?|first|atop|top)\b", q) and re.search(r"\b(?:division|conference|nl|al|nfc|afc)\b", q):
        return "leader"
    if (has_team(q) or re.search(r"\b(?:they|their)\b", q)) and re.search(r"\brecord\b", q):
        return "standings"
    if re.search(r"\bstandings\b", q) or (hint and re.search(r"\bstanding\b", q)) or (hint and re.search(r"\b(?:what|which) (?:place|position)|\brank(?:ed|ing)?\b|\bwhere\b.*\bstand\b", q)):
        return "standings"
    if hint and (re.search(r"\b(?:next (?:game|match)|play(?:ing)? next)\b", q)
                 or re.search(r"\bwhen\b.*\bplay\b", q)):
        return "next"
    if hint and re.search(r"\bhow\b.*\bdoing\b", q):
        return "score" if re.search(r"\b(?:game|tonight|right now)\b", q) else "overview"
    if is_score_query(q) or (hint and re.search(r"\b(?:who won|did .+ win|winning|losing|result)\b", q)):
        return "score"
    return None


def is_sports_query(query):
    return sports_kind(query) is not None


def with_team_context(query, context):
    if context and not has_team(query) and not re.search(r"\b(?:nl|al|nfc|afc)\b", query, re.I):
        if re.search(r"\b(?:they|their|them|division)\b", query, re.I):
            return f"{query} ({context['team']}, {context['league'].upper()})"
    return query
