"""Natural sports queries, typed standings and schedules, and per-sender context."""
from copy import deepcopy
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest

from bot import sports_details as details
from bot.sports import CLARIFY, sports_answer
from bot.sports_queries import sports_kind, with_team_context
from bot.web import needs_web
from bot.service import Decision
from tests.test_sports import NOW, event, page, service_clock


def team(ident, name, city, abbr):
    return {"id": ident, "name": name, "location": city, "abbreviation": abbr,
            "displayName": f"{city} {name}", "shortDisplayName": name}


SEASON = {"year": 2026, "startDate": "2026-02-19T08:00Z", "endDate": "2026-11-12T07:59Z"}

BREWERS = team("8", "Brewers", "Milwaukee", "MIL")
CUBS = team("16", "Cubs", "Chicago", "CHC")
REDS = team("17", "Reds", "Cincinnati", "CIN")


def row(t, wins, losses, gb, seed, points=0):
    values = {"wins": wins, "losses": losses, "ties": 0, "winPercent": wins/(wins+losses),
              "gamesBehind": gb, "divisionGamesBehind": gb, "playoffSeed": seed, "points": points}
    return {"team": t, "stats": [{"name": k, "value": v} for k,v in values.items()]}


def group():
    return {"name": "National League Central", "abbreviation": "NLC", "standings": {"entries": [
        row(BREWERS, 93, 57, 0, 1, 18), row(CUBS, 83, 67, 10, 4, 8), row(REDS, 70, 79, 22.5, 11, -4.5)]}}


def standing():
    return {"sports_detail": "standings", "league": "mlb", "season": 2026, "season_info": SEASON,
            "group": group(), "fetched_at": NOW.isoformat()}


def scheduled(state="pre", status="STATUS_SCHEDULED", offset=2):
    e = event(state, status, day=(NOW.date()+timedelta(days=offset)).isoformat())
    c = e["competitions"][0]
    c["timeValid"] = True
    c["competitors"][0]["team"] = REDS
    c["competitors"][1]["team"] = BREWERS
    return e


def next_page(e=None):
    return details._event_packet(e or scheduled(), "mlb", BREWERS, NOW)


@pytest.mark.parametrize("query,kind", [
    ("What place are the brewers in?", "standings"), ("What's the Brewers' division rank?", "standings"),
    ("Where do the Cubs stand?", "standings"), ("Brewers record?", "standings"),
    ("How are the Packers doing?", "overview"), ("How are the Packers doing in the game?", "score"),
    ("Who's leading the division?", "leader"), ("Who leads the NL Central?", "leader"),
    ("How many games behind are the Cubs?", "behind"), ("How far back are the Brewers?", "behind"),
    ("When do the Brewers play?", "next"),
    ("Who are the Bucks playing next?", "next"), ("Packers next game?", "next"),
    ("Did the Brewers win?", "score"), ("Who won Packers?", "score"), ("Are the Brewers winning?", "score"),
])
def test_natural_sports_phrases_route(query, kind):
    assert sports_kind(query) == kind
    assert needs_web(query)


@pytest.mark.parametrize("query", [
    "How are you doing?", "How is Michael doing?", "What place are my keys in?",
    "What's the best place to eat tonight?", "When does the band play next?",
    "How does division work?", "What's my credit score?", "How do football standings work?",
    "What is a standing wave?", "Where should I be standing?",
    "Can you rank these poems?", "What's my personal record?", "Who won the election?",
])
def test_ordinary_questions_do_not_become_sports_requests(query):
    assert sports_kind(query) is None


@pytest.mark.parametrize("query,expected", [
    ("What place are the brewers in?", "Brewers: listed 1st in NL Central, 93-57; lead by 10 games"),
    ("What place are the Cubs in?", "Cubs: listed 2nd in NL Central, 83-67; 10 games behind"),
    ("How many games behind are the Reds?", "Reds: listed 3rd in NL Central, 70-79; 22.5 games behind"),
    ("Who's leading the NL Central?", "Brewers: listed 1st in NL Central, 93-57; lead by 10 games"),
    ("How are the Brewers doing?", "Brewers: listed 1st in NL Central, 93-57; lead by 10 games"),
])
def test_real_stat_semantics_and_division_order(query, expected):
    answer, evidence = sports_answer([standing()], query, NOW, 125)
    assert answer == expected + " (ESPN 09/13)."
    assert evidence and evidence["league"] == "mlb"
    # Cubs' playoffSeed is 4, but their division position is 2, not 4.


@pytest.mark.parametrize("mutation", [
    lambda p: p.update(season=2027),
    lambda p: p.update(fetched_at=(NOW-timedelta(seconds=61)).isoformat()),
    lambda p: p["group"]["standings"]["entries"][0]["stats"][0].update(value="NaN"),
    lambda p: p["group"]["standings"]["entries"][0]["stats"][0].update(value=True),
    lambda p: p["group"]["standings"]["entries"][0]["stats"].append({"name":"wins", "value": 99}),
    lambda p: p["group"]["standings"]["entries"][0]["team"].update(shortDisplayName="@[Michael]"),
])
def test_bad_standings_are_not_broadcast(mutation):
    p = deepcopy(standing())
    mutation(p)
    assert sports_answer([p], "Brewers standings?", NOW, 125) == (details.UNAVAILABLE, None)


def test_wrong_division_is_not_answered_with_teams_actual_division():
    assert sports_answer([standing()], "Brewers NL East standings?", NOW, 125)[1] is None


def fake_fetch(url):
    league = url.split("/")[-2]
    if "/teams?" in url:
        return {"sports": [{"leagues": [{"slug":league,"teams": [{"team":t} for t in (BREWERS,CUBS,REDS)] if league=="mlb" else []}]}]}
    if "/standings?" in url:
        assert "season=2026" in url and "level=3" in url
        return {"season":SEASON,"children":[group()]}
    if "/teams/8" in url:
        return {"team": {**BREWERS,"nextEvent":[scheduled()]}}
    raise AssertionError(url)


@pytest.mark.parametrize("query", ["What place are the brewers in?", "Who leads NL Central?", "How many games behind are the Cubs?"])
def test_full_collection_to_rendering(query):
    pages = details.collect_details(query, fake_fetch, NOW)
    assert len(pages)==1
    assert sports_answer(pages, query, NOW, 125)[1]


def test_full_next_game_collection_preserves_date_and_opponent():
    pages = details.collect_details("When do the Brewers play next?", fake_fetch, NOW)
    assert len(pages)==1
    answer,evidence = sports_answer(pages,"When do the Brewers play next?",NOW,125)
    assert answer == "Next: Brewers at Reds, Tue 09/15 15:25 CDT (ESPN)."
    assert evidence["team_context"] == {"team":"Milwaukee Brewers","league":"mlb"}


def test_compact_next_event_without_name_needs_no_scoreboard_fallback():
    def fetch(url):
        data = fake_fetch(url)
        if "/teams/8" in url:
            for competitor in data["team"]["nextEvent"][0]["competitions"][0]["competitors"]:
                competitor["team"].pop("name")
        return data
    query = "When do the Brewers play next?"
    pages = details.collect_details(query, fetch, NOW)
    assert sports_answer(pages, query, NOW, 125)[1]


@pytest.mark.parametrize("e", [scheduled("post","STATUS_FINAL",-1), scheduled("pre","STATUS_POSTPONED"), scheduled(offset=-1)])
def test_next_event_cannot_be_finished_postponed_or_past(e):
    assert sports_answer([next_page(e)],"When do the Brewers play next?",NOW,125)[1] is None


def test_unknown_start_time_is_not_guessed():
    p = next_page()
    p["event"]["competitions"][0]["timeValid"] = False
    assert sports_answer([p],"When do the Brewers play next?",NOW,125)[1] is None


def test_finished_next_event_uses_small_daily_scoreboards():
    urls=[]
    def fetch(url):
        urls.append(url)
        if "/teams/8" in url:
            return {"team":{**BREWERS,"nextEvent":[scheduled("post","STATUS_FINAL",-1)]}}
        if "scoreboard?" in url:
            events = [scheduled(offset=1)] if "dates=20260914" in url else []
            return {"leagues":[{"slug":"mlb"}],"events":events}
        return fake_fetch(url)
    pages = details.collect_details("When do Brewers play next?",fetch,NOW)
    assert sports_answer(pages,"When do Brewers play next?",NOW,125)[1]
    assert all("schedule?" not in url and "dates=20260913-" not in url for url in urls)


def test_earlier_failed_day_cannot_be_skipped_for_later_game():
    def fetch(url):
        if "/teams/8" in url: return {"team":{**BREWERS,"nextEvent":[]}}
        if "scoreboard?" in url:
            return {"_error":"offline"} if "dates=20260913" in url else {"leagues":[{"slug":"mlb"}],"events":[scheduled(offset=1)]}
        return fake_fetch(url)
    pages = details.collect_details("When do Brewers play next?",fetch,NOW)
    assert sports_answer(pages,"When do Brewers play next?",NOW,125)[1] is None


@pytest.mark.parametrize("query",["When do they play next?","Who's leading the division?","MLB standings?"])
def test_missing_team_or_group_gets_clarification(query):
    pages=details.collect_details(query,fake_fetch,NOW)
    assert sports_answer(pages,query,NOW,125)==(CLARIFY,None)


def test_context_does_not_override_named_team_or_division():
    context={"team":"Milwaukee Brewers","league":"mlb"}
    assert "Milwaukee Brewers" in with_team_context("When do they play next?",context)
    assert with_team_context("When do the Packers play next?",context)=="When do the Packers play next?"
    assert with_team_context("Who leads the NL East division?",context)=="Who leads the NL East division?"


async def test_standings_and_followup_need_no_model(harness,service_clock):
    h=harness(web_enabled=True,global_burst=3,sender_burst=3)
    h.service.web.search=AsyncMock(side_effect=[[standing()],[next_page()]])
    assert await h.say("Michael: What place are the brewers in?") is Decision.ANSWERED
    assert "listed 1st in NL Central" in h.sent[0][1]
    assert await h.say("Michael: When do they play next?") is Decision.ANSWERED
    assert "Milwaukee Brewers, MLB" in h.service.web.search.call_args.args[0]
    assert "Next: Brewers at Reds" in h.sent[1][1]
    assert not h.backend.calls


async def test_followup_context_is_per_sender_and_expires(harness,service_clock,clock):
    h=harness(web_enabled=True,global_burst=5,sender_burst=5)
    h.service.web.search=AsyncMock(side_effect=[[standing()],[],[]])
    await h.say("Michael: Brewers standings?")
    await h.say("Alice: When do they play next?")
    assert h.service.web.search.await_count == 1
    clock.advance(601)
    await h.say("Michael: When do they play next?")
    assert h.service.web.search.await_count == 1


async def test_forget_clears_followup_context(harness,service_clock):
    h=harness(web_enabled=True,global_burst=4,sender_burst=4)
    h.service.web.search=AsyncMock(return_value=[standing()])
    await h.say("Michael: Brewers standings?")
    assert "Michael" in h.service._sports_context
    assert await h.say("Michael: /forget") is Decision.ANSWERED_FORGET
    assert "Michael" not in h.service._sports_context


async def test_standings_during_model_cooldown_and_repeated_queries(harness,service_clock):
    h=harness(web_enabled=True,global_burst=3,sender_burst=3)
    h.service._backend_retry_at=h.service._clock()+60
    h.service.web.search=AsyncMock(return_value=[standing()])
    for _ in range(3):
        assert await h.say("Michael: What place are the brewers in?") is Decision.ANSWERED
    assert len(h.sent)==3 and not h.backend.calls
