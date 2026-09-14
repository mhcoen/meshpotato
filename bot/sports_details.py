"""Structured standings and next-game answers; never infer facts from model text."""
from __future__ import annotations

import concurrent.futures
import re
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from bot.sports import (LEAGUES, MAX_AGE_S, CLARIFY, _name, _team, _team_source, _time,
                        local_now, matches, match_strength, parse_event, selected_leagues, words)
from bot.sports_queries import sports_kind

UNAVAILABLE = "I couldn't verify current sports information from ESPN."


def season_year(league, now):
    if league == "nfl":
        return now.year - (now.month <= 2)
    if league in {"nba", "nhl"}:
        return now.year + (now.month >= 10)
    return now.year


def _leagues(query):
    if re.search(r"\b(?:nfc|afc|national football conference|american football conference)\b", query, re.I):
        return ["nfl"]
    if re.search(r"\b(?:nl|al|national league|american league)\b", query, re.I):
        return ["mlb"]
    return selected_leagues(query)


def active_season(season, now):
    """A recent fetch does not make a completed season current."""
    start, end = _time(season["startDate"]), _time(season["endDate"])
    return (type(season["year"]) is int and start <= now <= end
            and timedelta(0) < end - start < timedelta(days=550))


def resolve_teams(query, fetch):
    def identify(league):
        try:
            data = fetch(f"https://site.api.espn.com/apis/site/v2/sports/{LEAGUES[league]}/{league}/teams?limit=100")
            group = next(g for g in data["sports"][0]["leagues"] if g["slug"] == league)
            found = []
            for row in group["teams"]:
                if matches(_team(row), query):
                    team = row["team"]
                    if not re.fullmatch(r"\d{1,8}", team["id"]):
                        raise ValueError("invalid team id")
                    found.append((league, {**_team_source(row), "id": team["id"]}))
            return found, None
        except (KeyError, TypeError, ValueError, OSError, AttributeError, StopIteration) as exc:
            return [], {"sports_error": f"{league}: team catalog unavailable ({type(exc).__name__})"}
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(identify, _leagues(query)))
    errors = [error for _, error in results if error]
    found = [item for matches_, _ in results for item in matches_]
    best = max((match_strength(_team({"team": t}), query) for _, t in found), default=0)
    return [item for item in found if match_strength(_team({"team": item[1]}), query) == best], errors


def label(group, league):
    name = _name(group["name"], 64)
    for full, short in (("National League", "NL"), ("American League", "AL"),
                        ("National Football Conference", "NFC"), ("American Football Conference", "AFC")):
        name = name.replace(full, short)
    return name if re.match(r"(?:NL|AL|NFC|AFC)\b", name) else f"{league.upper()} {name}"


def group_matches(group, league, query):
    return any(" " + words(value) + " " in " " + words(query) + " "
               for value in (group["name"], label(group, league)) if value)


def groups(data, depth=0):
    if depth > 4:
        raise ValueError("standings nesting")
    if data.get("standings", {}).get("entries"):
        yield data
    for child in data.get("children", [])[:32]:
        yield from groups(child, depth+1)


OT_NAMES = {"OTLosses", "otLosses", "overtimeLosses"}
STAT_NAMES = {"wins", "losses", "ties", "points", "winPercent", "gamesBehind", "divisionGamesBehind"} | OT_NAMES


def stats(row, league):
    result = {}
    for stat in row["stats"]:
        name = stat["name"]
        if name not in STAT_NAMES or (league != "nhl" and (name == "points" or name in OT_NAMES)):
            continue
        if isinstance(stat.get("value"), bool):
            raise ValueError("duplicate or invalid statistic")
        original_name = name
        if name in OT_NAMES:
            name = "OTLosses"
        value = Decimal(str(stat["value"]))
        if name in result:
            if original_name in OT_NAMES and value == result[name]:
                continue  # ESPN supplies both aliases in the same NHL row.
            raise ValueError("conflicting or duplicate statistic")
        if not value.is_finite() or not 0 <= value <= 1000:
            raise ValueError("invalid statistic")
        if name in {"wins", "losses", "ties", "OTLosses", "points"} and value != int(value):
            raise ValueError("fractional record")
        if name == "winPercent" and value > 1:
            raise ValueError("invalid percentage")
        result[name] = value
    if not {"wins", "losses"} <= result.keys():
        raise ValueError("missing record")
    return result


def _compact_group(group):
    return {"name": group["name"], "abbreviation": group.get("abbreviation", ""),
            "standings": {"entries": [{"team": {**_team_source(row), "id": row["team"]["id"]},
                                       "stats": [{"name": s["name"], "value": s.get("value")} for s in row["stats"] if s["name"] in STAT_NAMES]}
                                      for row in group["standings"]["entries"][:40]]}}


def _ordinal(rank):
    return str(rank) + ("th" if 10 < rank % 100 < 14 else {1:"st",2:"nd",3:"rd"}.get(rank % 10, "th"))


def _number(value):
    return format(value, "f").rstrip("0").rstrip(".") if value != int(value) else str(int(value))


def _standings_answer(page, query, now, available):
    league, group = page["league"], page["group"]
    if league not in _leagues(query) or not active_season(page["season_info"], now) or page["season"] != page["season_info"]["year"]:
        return UNAVAILABLE, None
    rows = group["standings"]["entries"]
    if not 1 <= len(rows) <= 40 or len({r["team"]["id"] for r in rows}) != len(rows):
        return UNAVAILABLE, None
    teams, values = [_team(r) for r in rows], [stats(r, league) for r in rows]
    primary = "points" if league == "nhl" else "winPercent"
    # Feed array order is not a rank (NBA and conference tables are unsorted).
    # Sort only within the selected group. Equal statistical keys share a rank;
    # this does not pretend to implement each league's playoff tiebreakers.
    division = (not re.search(r"\bconference\b", group["name"], re.I)
                and group["name"].lower() not in {"national league", "american league"})
    def rank_key(s):
        gb = "divisionGamesBehind" if division and "divisionGamesBehind" in s else "gamesBehind"
        return (-s[primary], s.get(gb, Decimal(0))) if league != "nhl" else (-s[primary],)
    ranked = sorted(zip(teams, values), key=lambda pair: rank_key(pair[1]))
    teams, values = map(list, zip(*ranked))
    if re.search(r"\b(?:nl|al|nfc|afc)\s+(?:east|west|central|north|south)\b|\b(?:eastern|western) conference\b|\b(?:national|american) league\b", query, re.I) and not group_matches(group, league, query):
        return UNAVAILABLE, None
    indexes = [i for i, t in enumerate(teams) if matches(t, query)]
    kind = sports_kind(query)
    if kind == "leader":
        if not indexes and not group_matches(group, league, query):
            return CLARIFY, None
        index = 0
    elif len(indexes) == 1:
        index = indexes[0]
    else:
        return CLARIFY, None
    s = values[index]
    record = f"{int(s['wins'])}-{int(s['losses'])}"
    if league == "nhl":
        record += f"-{int(s['OTLosses'])}"
    elif s.get("ties", 0):
        record += f"-{int(s['ties'])}"
    position = 1 + sum(rank_key(other) < rank_key(s) for other in values)
    tied = sum(rank_key(other) == rank_key(s) for other in values) > 1
    gb_key = "divisionGamesBehind" if division and "divisionGamesBehind" in s else "gamesBehind"
    gap = ""
    if league == "nhl":
        gap = f"; {int(s['points'])} points"
    elif gb_key in s:
        if index == 0 and len(values) > 1 and gb_key in values[1]:
            gap = "; tied for lead" if values[1][gb_key] == 0 else f"; lead by {_number(values[1][gb_key])} games"
        elif s[gb_key] > 0:
            gap = f"; {_number(s[gb_key])} games behind"
        else:
            gap = "; tied for lead"
    elif kind == "behind":
        gap = "; games-behind unavailable"
    for key in ("short", "abbreviation"):
        place = f"{'tied' if tied else 'listed'} {_ordinal(position)}"
        line = f"{teams[index][key]}: {place} in {label(group, league)}, {record}{gap} (ESPN {now:%m/%d})."
        if s["wins"] + s["losses"] + s.get("ties", 0) + s.get("OTLosses", 0) == 0:
            line = f"{teams[index][key]}: no completed games recorded this season, {record} (ESPN {now:%m/%d})."
        if len(line) <= available:
            return line, {"league": league, "fetched_at": page["fetched_at"], "url": f"https://www.espn.com/{league}/standings",
                          "team_context": {"team": teams[index]["display"], "league": league}}
    return UNAVAILABLE, None


def _event_packet(event, league, team, now):
    c = event["competitions"][0]
    status = c["status"]
    return {"sports_detail": "next", "league": league, "team": team, "fetched_at": now.isoformat(),
            "event": {"id": event["id"], "competitions": [{"date": c["date"], "timeValid": c.get("timeValid", event.get("timeValid", True)),
                "status": {"period": status.get("period"), "displayClock": status.get("displayClock"),
                           "type": {k:status["type"].get(k) for k in ("state", "name", "completed", "shortDetail")}},
                "competitors": [{"homeAway": t["homeAway"], "team": _team_source(t)} for t in c["competitors"]]}]}}


def _next_answer(page, query, now, available):
    league, event = page["league"], page["event"]
    team = _team({"team": page["team"]})
    if league not in _leagues(query) or not matches(team, query):
        return CLARIFY, None
    comp = event["competitions"][0]
    start = _time(comp["date"]).astimezone(now.tzinfo)
    if start < now or comp.get("timeValid") is not True:
        return UNAVAILABLE, None
    parsed = parse_event(event, league, f"{team['display']} {league} score {start:%Y-%m-%d}", now)
    if not parsed or parsed["state"] != "pre" or not parsed["status"].startswith("scheduled "):
        return UNAVAILABLE, None
    for key in ("short", "abbreviation"):
        names = [t[key] for t in parsed["teams"]]
        line = f"Next: {names[0]} at {names[1]}, {start:%a %m/%d %H:%M %Z} (ESPN)."
        if len(line) <= available:
            return line, {"league": league, "fetched_at": page["fetched_at"],
                          "url": f"https://www.espn.com/{league}/game/_/gameId/{parsed['id']}",
                          "team_context": {"team": team["display"], "league": league}}
    return UNAVAILABLE, None


def details_answer(pages, query, now, available):
    valid = []
    for page in pages:
        try:
            if not -5 <= (now - _time(page["fetched_at"])).total_seconds() <= MAX_AGE_S:
                continue
            if page.get("sports_detail") == "clarify":
                return CLARIFY, None
            if page.get("sports_detail") == "standings" and sports_kind(query) in {"standings", "behind", "leader", "overview"}:
                line, evidence = _standings_answer(page, query, now, available)
            elif page.get("sports_detail") == "next" and sports_kind(query) == "next":
                line, evidence = _next_answer(page, query, now, available)
            else:
                continue
            if evidence:
                valid.append((line, evidence))
        except (KeyError, TypeError, ValueError, AttributeError, IndexError, InvalidOperation):
            continue
    return valid[0] if len(valid) == 1 else (CLARIFY if len(valid) > 1 else UNAVAILABLE, None)


def collect_details(query, fetch, now=None):
    now = now or local_now()
    kind = sports_kind(query)
    # These endpoints describe the current season / next game, not historical
    # snapshots. Never silently answer an explicit old date with current data.
    if re.search(r"\b(?:yesterday|tomorrow|last|ago)\b|\b\d{4}-\d{2}-\d{2}\b", re.sub(r"\(as of .*?\)$", "", query), re.I):
        return []
    teams, errors = resolve_teams(query, fetch)
    if errors:
        return errors[:3]
    if len(teams) > 1:
        return [{"sports_detail": "clarify", "fetched_at": now.isoformat()}]
    if kind == "next":
        return _collect_next(query, teams, fetch, now)
    leagues = [teams[0][0]] if teams else _leagues(query)
    if not teams and (kind != "leader" or len(leagues) != 1):
        return [{"sports_detail": "clarify", "fetched_at": now.isoformat()}]
    league = leagues[0]
    season = season_year(league, now)
    years = re.findall(r"\b20\d{2}\b", re.sub(r"\(as of .*?\)$", "", query))
    if any(int(y) != season for y in years):
        return []
    level = 2 if re.search(r"\bconference\b|\b(?:national|american) league\b|\b(?:nl|al)\b", query, re.I) and not re.search(r"\b(?:east|west|central|north|south)\b", query, re.I) else 3
    try:
        def table(year):
            return fetch(f"https://site.api.espn.com/apis/v2/sports/{LEAGUES[league]}/{league}/standings?season={year}&level={level}")
        data = table(season)
        if data["season"]["year"] != season:
            return []
        if not active_season(data["season"], now):
            # Calendar heuristics can miss a league's exact opening date.
            # Check the adjacent season, still inside the parent's retrieval cap.
            season += 1 if now > _time(data["season"]["endDate"]) else -1
            if years and any(int(y) != season for y in years):
                return []
            data = table(season)
            if data["season"]["year"] != season or not active_season(data["season"], now):
                return []
        packets = []
        for group in groups(data):
            if teams:
                if not any(r["team"]["id"] == teams[0][1]["id"] for r in group["standings"]["entries"]):
                    continue
            elif not group_matches(group, league, query):
                continue
            packet = {"sports_detail": "standings", "league": league, "season": season,
                      "season_info": {k: data["season"][k] for k in ("year", "startDate", "endDate")},
                      "group": _compact_group(group), "fetched_at": now.isoformat()}
            if _standings_answer(packet, query, now, 1000)[1]:
                packets.append(packet)
        return packets[:3]
    except (KeyError, TypeError, ValueError, AttributeError, InvalidOperation):
        return [{"sports_error": f"{league}: standings unavailable"}]


def _collect_next(query, teams, fetch, now):
    if len(teams) != 1:
        return [{"sports_detail": "clarify", "fetched_at": now.isoformat()}]
    league, team = teams[0]
    def choose(events):
        packets = []
        for event in events:
            try:
                packet = _event_packet(event, league, team, now)
                if _next_answer(packet, query, now, 1000)[1]:
                    packets.append(packet)
            except (KeyError, TypeError, ValueError, AttributeError, IndexError):
                continue
        return sorted(packets, key=lambda p: _time(p["event"]["competitions"][0]["date"]))[:1]
    # Full-season team schedules can exceed 1 MB. Try the compact nextEvent
    # first, then small daily scoreboards when it still points to a finished game.
    detail = fetch(f"https://site.api.espn.com/apis/site/v2/sports/{LEAGUES[league]}/{league}/teams/{team['id']}")
    if detail.get("team", {}).get("id") == team["id"]:
        found = choose(detail["team"].get("nextEvent", []))
        if found:
            return found
    def daily(offset):
        day = now.date() + timedelta(days=offset)
        return fetch(f"https://site.api.espn.com/apis/site/v2/sports/{LEAGUES[league]}/{league}/scoreboard?dates={day:%Y%m%d}&limit=100")
    for start in range(-1, 15, 3):
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(daily, range(start, min(start+3, 15))))
        if any(not isinstance(d.get("events"), list) or not any(l.get("slug") == league for l in d.get("leagues", [])) for d in results):
            return [{"sports_error": f"{league}: next-game coverage incomplete"}]
        found = choose([e for d in results for e in d["events"]])
        if found:
            return found
    return []
