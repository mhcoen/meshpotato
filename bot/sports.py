"""Typed ESPN scoreboard lookup and deterministic, bounded score responses.

No model supplies or rewrites scores. The provider is authoritative for the
reported snapshot, not a guarantee that its live feed has no delay.
"""
from __future__ import annotations

import concurrent.futures
import re
import os
from zoneinfo import ZoneInfo
from datetime import datetime, timedelta
from bot.sports_names import TEAM_ALIASES

LEAGUES = {"nfl": "football", "nba": "basketball", "wnba": "basketball", "mlb": "baseball", "nhl": "hockey"}
CLARIFY = "Which team and league do you mean? For multiple games, include the opponent or date."
UNAVAILABLE = "I couldn't verify that score from ESPN's scoreboard; include the team and game date."
MAX_AGE_S = 60


def local_now() -> datetime:
    """Keep timezone rules, not just today's offset, for dated game start times."""
    try:
        if os.environ.get("TZ"):
            zone = ZoneInfo(os.environ["TZ"])
        else:
            with open("/etc/localtime", "rb") as source:
                zone = ZoneInfo.from_file(source)
        return datetime.now(zone)
    except (OSError, ValueError, KeyError):
        return datetime.now().astimezone()


def is_score_query(query: str) -> bool:
    if re.search(r"\b(?:credit|test|exam|sat|act|scrabble|video game|high score|define|meaning|mean)\b|\bhow (?:do|does|to)\b", query, re.I):
        return False
    return bool(re.search(r"\bscores?\b|\bwho won\b.*\b(?:game|match|nfl|nba|wnba|mlb|nhl)\b", query, re.I))


def words(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.lower()))


def requested_date(query: str, now: datetime):
    # search_query appends an as-of date; it is context, not an explicit request.
    query = re.sub(r"\(as of \d{4}-\d{2}-\d{2}\)\s*$", "", query)
    dates = re.findall(r"\b\d{4}-\d{2}-\d{2}\b", query)
    if len(set(dates)) > 1:
        raise ValueError("ambiguous date")
    if dates:
        return datetime.strptime(dates[0], "%Y-%m-%d").date()
    # Other named/relative dates must not silently become today's game.
    if re.search(r"\b(?:last|next|ago|monday|tuesday|wednesday|thursday|friday|saturday|sunday|january|february|march|april|may|june|july|august|september|october|november|december)\b|\b\d{1,2}/\d{1,2}\b", query, re.I):
        raise ValueError("use an ISO game date")
    if re.search(r"\byesterday\b", query, re.I):
        return now.date() - timedelta(days=1)
    if re.search(r"\btomorrow\b", query, re.I):
        return now.date() + timedelta(days=1)
    return now.date()


def selected_leagues(query: str) -> list[str]:
    named = [league for league in LEAGUES if re.search(r"\b" + league + r"\b", query, re.I)]
    if named:
        return named
    sports = {"football": ["nfl"], "basketball": ["nba", "wnba"], "baseball": ["mlb"], "hockey": ["nhl"]}
    for sport, leagues in sports.items():
        if re.search(r"\b" + sport + r"\b", query, re.I):
            return leagues
    return list(LEAGUES)


def _time(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp requires timezone")
    return parsed


def _name(value, limit=40):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 .'-]{0," + str(limit - 1) + r"}", value):
        raise ValueError("invalid team name")
    return value


def _team(competitor):
    team = competitor["team"]
    # Keep only typed display fields, never links or free-form provider text.
    return {"name": _name(team.get("name") or team["shortDisplayName"]), "display": _name(team["displayName"]),
            "short": _name(team.get("shortDisplayName") or team["name"]),
            "abbreviation": _name(team["abbreviation"], 5),
            "location": _name(team["location"])}


def matches(team, query):
    return match_strength(team, query) > 0


def match_strength(team, query):
    normalized = " " + words(query) + " "
    strengths = {"display": 3, "name": 2, "short": 2, "location": 1}
    found = [strengths[key] for key, value in team.items()
             if key in strengths and " " + words(value) + " " in normalized]
    if any(canonical == words(team["display"]) and " " + alias + " " in normalized
           for alias, canonical in TEAM_ALIASES.items()):
        found.append(2)
    if re.search(r"\b" + re.escape(team["abbreviation"]) + r"\b", query):
        found.append(2)
    return max(found, default=0)


def parse_event(event: dict, league: str, query: str, now: datetime) -> dict | None:
    """Validate one competition; malformed events never supply partial scores."""
    try:
        if league not in selected_leagues(query) or not re.fullmatch(r"\d{1,12}", event["id"]):
            return None
        competitions = event["competitions"]
        if len(competitions) != 1:
            return None
        comp = competitions[0]
        start = _time(comp["date"]).astimezone(now.tzinfo)
        if start.date() != requested_date(query, now):
            return None
        competitors = comp["competitors"]
        if len(competitors) != 2 or {t["homeAway"] for t in competitors} != {"home", "away"}:
            return None
        competitors = sorted(competitors, key=lambda c: c["homeAway"])
        teams = [_team(c) for c in competitors]
        if teams[0]["display"] == teams[1]["display"] or not any(matches(t, query) for t in teams):
            return None
        if re.search(r"\b(?:vs|versus|against)\b", query, re.I) and not all(matches(t, query) for t in teams):
            return None
        status = comp["status"]
        typ = status["type"]
        state, name = typ["state"], typ["name"]
        if state in {"in", "post"} and start > now + timedelta(minutes=5):
            return None
        scores = []
        if state in {"in", "post"}:
            for c in competitors:
                value = c["score"]
                if not isinstance(value, str) or not re.fullmatch(r"\d{1,3}", value):
                    return None
                scores.append(int(value))
        if name in {"STATUS_POSTPONED", "STATUS_CANCELED", "STATUS_CANCELLED", "STATUS_DELAYED", "STATUS_SUSPENDED"}:
            label = name.removeprefix("STATUS_").lower()
        elif state == "pre" and name == "STATUS_SCHEDULED" and typ["completed"] is False:
            label = "scheduled " + start.strftime("%H:%M %Z")
        elif state == "post" and name in {"STATUS_FINAL", "STATUS_FINAL_OVERTIME", "STATUS_FINAL_SO"} and typ["completed"] is True:
            label = "final"
        elif state == "in" and typ["completed"] is False:
            period = status["period"]
            if type(period) is not int or not 1 <= period <= 30:
                return None
            if name == "STATUS_HALFTIME" and league in {"nfl", "nba", "wnba"}:
                label = "halftime"
            elif name == "STATUS_END_PERIOD" and league != "mlb":
                label = f"end {'P' if league == 'nhl' else 'Q'}{period}"
            elif name == "STATUS_SHOOTOUT" and league == "nhl":
                label = "shootout"
            elif name == "STATUS_IN_PROGRESS":
                if league == "mlb":
                    detail = typ.get("shortDetail", "")
                    match = re.fullmatch(r"(Top|Bot|Bottom|Mid|End) (\d{1,2})(?:st|nd|rd|th)?", detail, re.I)
                    if not match or int(match[2]) != period:
                        return None
                    label = f"{match[1].lower()} {period}"
                else:
                    clock = status["displayClock"]
                    if not isinstance(clock, str) or not re.fullmatch(r"\d{1,2}:[0-5]\d(?:\.\d)?|\d{1,2}\.\d", clock):
                        return None
                    regulation = 3 if league == "nhl" else 4
                    part = ("P" if league == "nhl" else "Q") + str(period) if period <= regulation else f"OT{period - regulation}"
                    label = f"{part} {clock}"
            else:
                return None
        else:
            return None
        return {"teams": teams, "scores": scores, "status": label, "date": start.strftime("%m/%d"),
                "id": event["id"], "league": league, "state": state}
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        return None


def collect_scores(query: str, fetch, now: datetime | None = None) -> list[dict]:
    now = now or local_now()
    try:
        day = requested_date(query, now)
    except ValueError:
        return []
    leagues = selected_leagues(query)
    # Resolve the team before requesting scoreboards. Catalogs are small and
    # prevent an unrelated league's large game feed from blocking this request.
    if len(leagues) > 1:
        def identify(league):
            try:
                data = fetch(f"https://site.api.espn.com/apis/site/v2/sports/{LEAGUES[league]}/{league}/teams?limit=100")
                groups = data["sports"][0]["leagues"]
                catalog = next(g for g in groups if g["slug"] == league)
                return league, any(matches(_team(t), query) for t in catalog["teams"]), None
            except (KeyError, TypeError, ValueError, OSError, AttributeError, StopIteration) as exc:
                return league, False, f"{league}: team catalog unavailable ({type(exc).__name__})"
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
            identified = list(pool.map(identify, leagues))
        errors = [{"sports_error": error} for _, _, error in identified if error]
        if errors:
            return errors[:3]
        leagues = [league for league, found, _ in identified if found]
    if not leagues:
        return []
    # Adjacent provider dates cover local timezone boundaries. Request each day
    # separately to keep baseball's three-day feed below the 1 MB response cap.
    targets = [(league, (day + timedelta(days=offset)).strftime("%Y%m%d"))
               for league in leagues for offset in (-1, 0, 1)]
    def collect(target):
        league, dates = target
        try:
            data = fetch(f"https://site.api.espn.com/apis/site/v2/sports/{LEAGUES[league]}/{league}/scoreboard?dates={dates}&limit=100")
            if data.get("_error"):
                return [{"sports_error": f"{league}: {str(data['_error'])[:120]}"}]
            if not any(l.get("slug") == league for l in data.get("leagues", [])) or not isinstance(data.get("events"), list):
                return [{"sports_error": f"{league}: invalid scoreboard"}]
            found = []
            for event in data.get("events", [])[:100]:
                if parse_event(event, league, query, now):
                    # Parent validates again before formatting. Limit transferred
                    # data to the fields actually consumed by the parser.
                    c = event["competitions"][0]
                    comp = {"date": c["date"], "status": c["status"], "competitors": [
                        {"homeAway": t["homeAway"], "score": t.get("score"), "team": _team_source(t)} for t in c["competitors"]]}
                    found.append({"sports": True, "league": league, "event": {"id": event["id"], "competitions": [comp]},
                                  "fetched_at": now.isoformat()})
            return found
        except (KeyError, TypeError, ValueError, OSError, AttributeError) as exc:
            return [{"sports_error": f"{league}: {type(exc).__name__}"}]
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        groups = list(pool.map(collect, targets))
    pages = [page for group in groups for page in group]
    # Incomplete league coverage could hide a second team with the same name.
    errors = [p for p in pages if "sports_error" in p]
    if errors:
        return errors[:3]
    unique = {}
    for page in pages:
        key = (page["league"], page["event"]["id"])
        if key in unique and unique[key] != page:
            return [{"sports_error": "conflicting scoreboard snapshots"}]
        unique[key] = page
    if len(unique) > 3:
        return [{"sports_error": "too many matching games; specify league and opponent"}]
    return list(unique.values())


def _team_source(competitor):
    t = competitor["team"]
    return {k: t.get(k) for k in ("name", "displayName", "shortDisplayName", "abbreviation", "location")}


def score_answer(pages: list[dict], query: str, now: datetime, available: int):
    """Return a deterministic line and evidence metadata, or a fixed notice."""
    candidates = {}
    for page in pages:
        try:
            if page.get("sports") is not True or not -5 <= (now - _time(page["fetched_at"])).total_seconds() <= MAX_AGE_S:
                continue
            event = parse_event(page["event"], page["league"], query, now)
            if event:
                key = (event["league"], event["id"])
                if key in candidates and candidates[key][0] != event:
                    return UNAVAILABLE, None
                candidates[key] = (event, page["fetched_at"])
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
    if not candidates:
        return UNAVAILABLE, None
    # Prefer matches to both named teams (opponent disambiguates city nicknames).
    best = max(sum(matches(t, query) for t in e["teams"]) for e, _ in candidates.values())
    choices = [(e, stamp) for e, stamp in candidates.values() if sum(matches(t, query) for t in e["teams"]) == best]
    if len(choices) != 1:
        return CLARIFY, None
    event, stamp = choices[0]
    fetched = _time(stamp).astimezone(now.tzinfo)
    for name_key in ("short", "abbreviation"):
        teams = event["teams"]
        names = [t[name_key] for t in teams]
        if event["scores"]:
            matchup = ", ".join(f"{n} {s}" for n, s in zip(names, event["scores"]))
        else:
            matchup = " vs ".join(names)
        line = f"{matchup}; {event['status']}, {event['date']} (ESPN {fetched:%H:%M %Z})."
        if len(line) <= available:
            return line, {"league": event["league"], "game_id": event["id"], "fetched_at": stamp,
                          "url": f"https://www.espn.com/{event['league']}/game/_/gameId/{event['id']}",
                          "team_context": team_context(event["teams"], query, event["league"])}
    return UNAVAILABLE, None


def team_context(teams, query, league):
    named = [t for t in teams if matches(t, query)]
    return {"team": named[0]["display"], "league": league} if len(named) == 1 else None


def collect_sports(query, fetch):
    from bot.sports_queries import sports_kind
    from bot.sports_details import collect_details
    return collect_scores(query, fetch) if sports_kind(query) == "score" else collect_details(query, fetch)


def sports_answer(pages, query, now, available):
    from bot.sports_queries import sports_kind
    from bot.sports_details import details_answer
    return score_answer(pages, query, now, available) if sports_kind(query) == "score" else details_answer(pages, query, now, available)


def sports_failure(query):
    from bot.sports_queries import sports_kind
    return UNAVAILABLE if sports_kind(query) == "score" else "I couldn't verify that from ESPN; include the team or division and league."
