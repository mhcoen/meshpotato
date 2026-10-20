"""Sports intents and bounded follow-up wording; no model or network routing."""
import re
from bot.sports_names import TEAM_NAMES, TEAM_ALIASES, AMBIGUOUS_ALIASES

# These nicknames also name ordinary things. Lowercase use needs sports context.
COMMON_NAMES = frozenset('sun sky heat wild fire bulls rays kings jazz wings reds twins magic thunder lightning stars sparks dream liberty fever storm mercury aces nets pelicans hornets hawks rockets warriors giants rangers guardians athletics royals angels tigers pirates cardinals orioles blue jays saints jets bears lions panthers falcons eagles ravens commanders titans chargers colts browns bills dolphins ducks senators predators avalanche islanders devils kraken capitals'.split())
NONSPORT = re.compile(r"\b(?:credit|exam|school|scrabble|video game|define|meaning|musical|resume|poem|band|concert|lawsuit|court|package|parcel|market|stocks|radio|antenna|bar|restaurant)\b|\bhow (?:do|does|to)\b.*\b(?:work|calculate|score)\b", re.I)
SCOPE = re.compile(r"\b(?:nfl|nba|wnba|mlb|nhl|football|basketball|baseball|hockey|game|match|standings|division|conference)\b", re.I)


def series_name(query):
    names = {
        'ALCS': r'\balcs\b|\bamerican league championship series\b',
        'NLCS': r'\bnlcs\b|\bnational league championship series\b',
        'World Series': r'\bworld series\b',
        'LCS': r'\blcs\b|(?<!american )(?<!national )\bleague championship series\b',
    }
    found = [name for name, pattern in names.items() if re.search(pattern, query, re.I)]
    return found[0] if len(found) == 1 else None


def dated_game(query):
    query = re.sub(r'\(as of .*?\)\s*$', '', query, flags=re.I)
    return bool(re.search(r'\b(?:today|tonight|yesterday|tomorrow)\b|\b\d{4}-\d{2}-\d{2}\b', query, re.I))


def series_definition(query):
    name = series_name(query)
    if not name or not re.search(r'\b(?:what (?:is|are)|stand for|mean|define|explain)\b', query, re.I):
        return ''
    if re.search(r'\b(?:scores?|time|when|tonight|today|tomorrow|yesterday|winning|won|playing|schedule|standings|record|winner|champion|date|teams|odds|results?|current)\b', query, re.I):
        return ''
    return {
        'ALCS': 'ALCS is the American League Championship Series; its winner advances to the World Series.',
        'NLCS': 'NLCS is the National League Championship Series; its winner advances to the World Series.',
        'LCS': 'LCS means League Championship Series: ALCS for the American League, NLCS for the National League.',
        'World Series': 'The World Series is MLB\'s best-of-seven championship between the American and National League champions.',
    }[name]


def normalized(query):
    return " ".join(re.findall(r"[a-z0-9]+", query.lower()))


def has_team(query):
    text = " " + normalized(query) + " "
    if any(" " + alias + " " in text for alias in TEAM_ALIASES.keys() | AMBIGUOUS_ALIASES.keys()):
        return True
    for name in TEAM_NAMES:
        if " " + normalized(name) + " " not in text:
            continue
        if name not in COMMON_NAMES or SCOPE.search(query) or re.search(r"\b(?:record|score|play|playing|rank)\b", query, re.I):
            return True
        if any(m.group()[0].isupper() for m in re.finditer(r"\b" + re.escape(name) + r"\b", query, re.I)):
            return True
    return False


def asks_sports_capabilities(query):
    """A general ability question has no named team or requested game date."""
    return (not has_team(query)
            and bool(re.search(r"^(?:can|could|do|are) you\b.*\b(?:sports|scores|standings|schedules)\b", query.strip(), re.I))
            and not re.search(r"\b(?:today|tonight|yesterday|tomorrow)\b|\b\d{4}-\d{2}-\d{2}\b", query, re.I))


def sports_kind(query, *, has_context=False):
    from bot.sports import is_score_query
    if NONSPORT.search(query) or series_definition(query):
        return None
    if asks_sports_capabilities(query):
        return None
    q = query.lower().replace("’", "'")
    team = has_team(query)
    series = series_name(query)
    hint = team or bool(series) or bool(re.search(r"\b(?:nfl|nba|wnba|mlb|nhl)\b", q)) or (
        has_context and bool(re.search(r'\b(?:they|their|them|game|match|division|score|winning|losing|record)\b', q)))
    next_game = bool(re.search(r"\b(?:next (?:game|match)|play(?:ing)? next)\b|\bwhen\b.*\bplay\b|\b(?:when|time|schedule|start)\b.*\b(?:game|match)\b", q))
    if series and re.search(r'\b(?:when|time|schedule|start|next)\b', q):
        return 'game' if dated_game(q) else 'series_next'
    if hint and next_game and re.search(r"\b(?:record|standings)\b", q):
        return "record_next"
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
    if hint and next_game:
        # A question about "the game" concerns today's game, including one
        # already in progress. Only explicit next-game wording advances dates.
        if dated_game(q) or re.search(r'\b(?:time|when)\b.*\b(?:game|match)\b', q) and not re.search(r'\bnext\b', q):
            return 'game'
        return "next"
    if not hint and next_game and re.search(r"\b(?:they|their|them)\b", q):
        return "next"  # Missing team needs clarification, never a model-invented date.
    if hint and re.search(r"\bhow\b.*\bdoing\b", q):
        return "score" if re.search(r"\b(?:game|tonight|right now)\b", q) else "overview"
    if is_score_query(q) or (hint and re.search(r"\b(?:who won|did .+ win|winning|losing|result)\b", q)):
        return "score"
    if hint and dated_game(q) and re.search(r'\b(?:game|match)\b', q):
        return 'score'
    if re.fullmatch(r'next (?:game|match)[?.! ]*', q):
        return 'next'
    return None


def is_sports_query(query):
    return sports_kind(query) is not None


def with_team_context(query, context):
    if series_name(query):
        return query  # A competition is not restricted to the previously discussed team.
    # A shared nickname can be resolved by an already established league, without
    # importing any old scores. Explicit new league/sport wording always wins.
    if context and not re.search(r"\b(?:nfl|nba|wnba|mlb|nhl|football|baseball|basketball|hockey)\b", query, re.I):
        short = normalized(context['team']).split()[-1]
        qualified = re.search(r'\b(?:sf|san francisco|ny|new york|boston|chicago|red sox|white sox|texas|arizona|st\.? louis|carolina|florida|los angeles|sacramento|winnipeg)\b', query, re.I)
        if not qualified and short in {'giants', 'jets', 'rangers', 'panthers', 'cardinals', 'kings', 'sox'} and re.search(r'\b'+short+r'\b', query, re.I):
            query = f"{query} ({context['team']}, {context['league'].upper()})"
    if (context and sports_kind(query, has_context=True)
            and not re.search(r"\b(?:nl|al|nfc|afc|nfl|nba|wnba|mlb|nhl)\b", query, re.I)):
        if (not has_team(query) or re.search(r'\b(?:they|their|them)\b', query, re.I)) and normalized(context['team']) not in normalized(query):
            return f"{query} ({context['team']}, {context['league'].upper()})"
    return query
