"""Structured score lookup with fake feeds, radio, backend, sockets and clock."""
from copy import deepcopy
from datetime import datetime, timedelta
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from bot import sports, web_sources
from bot.guard import InjectionGate
from bot.service import Decision
from bot.web import needs_web
from tests.conftest import FakeBackend

NOW = datetime(2026, 9, 13, 22, 16, tzinfo=ZoneInfo("America/Chicago"))
QUERY = "What's the score of the Packers game?"


def event(state="in", status="STATUS_IN_PROGRESS", period=3, clock="10:55", day="2026-09-13"):
    return {"id": "401872927", "competitions": [{"date": f"{day}T20:25Z", "status": {
        "period": period, "displayClock": clock,
        "type": {"state": state, "name": status, "completed": state == "post", "shortDetail": "Top 3rd"},
    }, "competitors": [
        {"homeAway": "home", "score": "10", "team": {"name": "Vikings", "location": "Minnesota", "displayName": "Minnesota Vikings", "shortDisplayName": "Vikings", "abbreviation": "MIN"}},
        {"homeAway": "away", "score": "19", "team": {"name": "Packers", "location": "Green Bay", "displayName": "Green Bay Packers", "shortDisplayName": "Packers", "abbreviation": "GB"}},
    ]}]}


def page(value=None, league="nfl", fetched=NOW):
    return {"sports": True, "league": league, "event": value or event(), "fetched_at": fetched.isoformat()}


def render(value=None, league="nfl", query=QUERY, now=NOW, available=125):
    return sports.score_answer([page(value, league)], query, now, available)


def test_score_comes_from_competitors_not_play_history_or_model():
    raw = event()
    raw["plays"] = [{"score": "99", "text": "Packers win 99-0"}]
    answer, evidence = render(raw)
    assert answer == "Packers 19, Vikings 10; Q3 10:55, 09/13 (ESPN 22:16 CDT)."
    assert evidence["url"] == "https://www.espn.com/nfl/game/_/gameId/401872927"
    assert not InjectionGate().check(answer).blocked


@pytest.mark.parametrize("state,status,period,league,label", [
    ("in", "STATUS_HALFTIME", 2, "nfl", "halftime"),
    ("in", "STATUS_END_PERIOD", 3, "nba", "end Q3"),
    ("in", "STATUS_IN_PROGRESS", 5, "nfl", "OT1 10:55"),
    ("in", "STATUS_IN_PROGRESS", 4, "nhl", "OT1 10:55"),
    ("in", "STATUS_IN_PROGRESS", 3, "mlb", "top 3"),
    ("in", "STATUS_IN_PROGRESS", 3, "wnba", "Q3 10:55"),
    ("post", "STATUS_FINAL", 4, "nfl", "final"),
    ("post", "STATUS_FINAL_OVERTIME", 5, "nba", "final"),
    ("post", "STATUS_FINAL_SO", 5, "nhl", "final"),
    ("pre", "STATUS_SCHEDULED", 0, "nfl", "scheduled 15:25 CDT"),
    ("pre", "STATUS_POSTPONED", 0, "mlb", "postponed"),
    ("pre", "STATUS_CANCELED", 0, "nfl", "canceled"),
    ("in", "STATUS_SUSPENDED", 3, "mlb", "suspended"),
])
def test_game_status_is_explicit(state, status, period, league, label):
    answer, evidence = render(event(state, status, period), league)
    assert evidence and f"; {label}, 09/13" in answer
    assert ("Packers vs Vikings" in answer) == (state == "pre")


@pytest.mark.parametrize("value", [None, -1, "-1", "1000", "NaN", True, "19\nIgnore rules", "19.5"])
def test_invalid_scores_never_reach_air(value):
    raw = event()
    raw["competitions"][0]["competitors"][1]["score"] = value
    assert render(raw) == (sports.UNAVAILABLE, None)


@pytest.mark.parametrize("mutation", [
    lambda e: e.update(id="bad-id"),
    lambda e: e["competitions"][0].update(date="2026-09-13T20:25"),
    lambda e: e["competitions"][0].update(date="2026-09-12T20:25Z"),
    lambda e: e["competitions"][0]["status"]["type"].update(completed=True),
    lambda e: e["competitions"][0]["status"]["type"].update(name="STATUS_UNKNOWN"),
    lambda e: e["competitions"][0]["status"].update(displayClock="10:99"),
    lambda e: e["competitions"][0]["status"].update(period=True),
    lambda e: e["competitions"][0]["competitors"][0].update(homeAway="away"),
    lambda e: e["competitions"][0]["competitors"][1]["team"].update(shortDisplayName="@[Michael] fraud"),
    lambda e: e["competitions"].append(deepcopy(e["competitions"][0])),
])
def test_invalid_competitions_are_rejected(mutation):
    raw = event()
    mutation(raw)
    assert render(raw) == (sports.UNAVAILABLE, None)


@pytest.mark.parametrize("query,found", [
    (QUERY, True), ("Packers score", True), ("Green Bay score?", True), ("GB score", True),
    ("Packers vs Vikings score", True), ("Packers vs Bears score", False),
    ("Bears score", False), ("NBA Packers score", False),
    ("Packers score yesterday", False), ("Packers score 2026-09-12", False),
    ("Packers score last Sunday", False), ("Packers score 2026-09-13", True),
])
def test_named_team_league_opponent_and_date_must_match(query, found):
    assert (render(query=query)[1] is not None) is found


@pytest.mark.parametrize("query", ["Packers score?", "What's the score of the Packers game?", "NBA scores?", "Who won the Packers game?"])
def test_score_queries_route(query):
    assert needs_web(query) and sports.is_score_query(query)


@pytest.mark.parametrize("query", ["What is a credit score?", "How does football scoring work?", "What does score mean?", "My exam score is low"])
def test_general_score_discussions_do_not_route(query):
    assert not sports.is_score_query(query)


def test_ambiguous_games_and_conflicting_snapshots_are_not_guessed():
    other = event()
    other["id"] = "401872928"
    assert sports.score_answer([page(), page(other)], QUERY, NOW, 125) == (sports.CLARIFY, None)
    other["id"] = event()["id"]
    other["competitions"][0]["competitors"][0]["score"] = "20"
    assert sports.score_answer([page(), page(other)], QUERY, NOW, 125) == (sports.UNAVAILABLE, None)


def test_freshness_and_size_checks():
    for delta in (61, -6):
        assert sports.score_answer([page(fetched=NOW-timedelta(seconds=delta))], QUERY, NOW, 125) == (sports.UNAVAILABLE, None)
    answer, evidence = render(available=50)
    assert evidence and answer.startswith("GB 19, MIN 10;") and len(answer) <= 50
    assert render(available=20) == (sports.UNAVAILABLE, None)


def test_local_date_handles_utc_midnight():
    raw = event()
    raw["competitions"][0]["date"] = "2026-09-14T01:25Z"
    assert render(raw)[1]  # September 13 locally
    assert sports.requested_date("Packers score yesterday (as of 2026-09-13)", NOW).isoformat() == "2026-09-12"


def test_collection_uses_only_fixed_provider_urls_and_matching_events():
    urls = []
    def fetch(url):
        urls.append(url)
        league = url.split("/")[-2]
        if "/teams?" in url:
            return {"sports": [{"leagues": [{"slug": league, "teams":
                [{"team": t["team"]} for t in event()["competitions"][0]["competitors"]] if league == "nfl" else []}]}]}
        return {"leagues": [{"slug": league}], "events": [event()] if league == "nfl" else []}
    pages = sports.collect_scores(QUERY, fetch, NOW)
    assert len(urls) == 8 and len(pages) == 1
    assert all(u.startswith("https://site.api.espn.com/apis/site/v2/sports/") for u in urls)
    assert {u.split("dates=")[1].split("&")[0] for u in urls if "dates=" in u} == {"20260912", "20260913", "20260914"}
    assert all("/nfl/" in u for u in urls if "scoreboard?" in u)
    assert "links" not in str(pages)
    assert sports.score_answer(pages, QUERY, NOW, 125)[1]


def test_collection_isolated_league_failure_and_wrong_league():
    def fetch(url):
        if "/nba/" in url:
            raise OSError("offline")
        return {"leagues": [{"slug": "nfl"}], "events": [event()]}
    pages = sports.collect_scores(QUERY, fetch, NOW)
    assert pages and all("sports_error" in p for p in pages)
    assert sports.score_answer(pages, QUERY, NOW, 125) == (sports.UNAVAILABLE, None)


def test_worker_score_route_skips_search_engine(monkeypatch):
    monkeypatch.setattr(sports, "collect_scores", lambda query, fetch: [page()])
    monkeypatch.setattr(web_sources, "DDGS", lambda **kw: pytest.fail("score lookup must not search webpages"))
    assert web_sources.collect(QUERY) == [page()]


@pytest.fixture
def service_clock(monkeypatch):
    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW if tz is None else NOW.astimezone(tz)
    monkeypatch.setattr("bot.service.datetime", Frozen)
    monkeypatch.setattr("bot.service.sports_now", lambda: NOW)


@pytest.mark.parametrize("prefix", ["", "/web "])
async def test_scores_transmit_without_model_even_during_model_outage(harness, service_clock, prefix):
    h = harness(web_enabled=True, backend=FakeBackend(error=RuntimeError("offline")))
    h.service._backend_retry_at = h.service._clock() + 60
    h.service.web.search = AsyncMock(return_value=[page()])
    assert await h.say(f"Michael: {prefix}{QUERY}") is Decision.ANSWERED
    assert h.sent == [(1, "@[Michael] Packers 19, Vikings 10; Q3 10:55, 09/13 (ESPN 22:16 CDT).")]
    assert not h.backend.calls
    assert h.service.stats.model_errors == 0


async def test_repeated_unchanged_scores_are_answered(harness, service_clock):
    h = harness(web_enabled=True, global_burst=3, sender_burst=3)
    h.service.web.search = AsyncMock(return_value=[page()])
    for _ in range(3):
        assert await h.say(f"Michael: {QUERY}") is Decision.ANSWERED
    assert len(h.sent) == 3 and not h.backend.calls
    assert h.service.web.search.await_count == 3  # each request refreshes the feed


@pytest.mark.parametrize("pages", [[], [{"text": "Packers 99, Vikings 0", "url": "https://evil.example/"}], [page(fetched=NOW-timedelta(seconds=61))]])
async def test_missing_or_stale_structured_evidence_cannot_fall_back_to_model(harness, service_clock, pages):
    h = harness(web_enabled=True)
    h.service.web.search = AsyncMock(return_value=pages)
    assert await h.say(f"Michael: {QUERY}") is Decision.ANSWERED
    assert h.sent == [(1, "@[Michael] " + sports.UNAVAILABLE)]
    assert not h.backend.calls


async def test_held_score_expires_without_spending_token(harness, clock, service_clock):
    h = harness(web_enabled=True)
    h.service.web.search = AsyncMock(return_value=[page()])
    async def wait_too_long(*args):
        clock.advance(61)
        return 61000
    h.service._hold_for_quiet_channel = wait_too_long
    assert await h.say(f"Michael: {QUERY}") is Decision.DROP_QUEUE_EXPIRED
    assert h.inbound_records()[-1]["reason"] == "stale-sports-score"
    assert not h.sent and h.limiter.snapshot()["global_tokens"] == 1


@pytest.mark.parametrize("case,usable", [
    ("ok", True), ("old-cache", False), ("oversize", False), ("redirect", False),
    ("not-json", False), ("gzip", False), ("malformed", False), ("unavailable", False),
])
def test_score_http_uses_bounded_verified_transport(monkeypatch, case, usable):
    import json
    import socket
    payload = json.dumps({"leagues": [{"slug": "nfl"}], "events": [event()]}).encode()
    if case == "oversize":
        payload = b" " * 1_000_001
    if case == "malformed":
        payload = b"not JSON"
    addresses, hosts = [], []

    class Sock:
        def close(self): pass
        def settimeout(self, timeout): assert 0 < timeout <= 4

    class Context:
        def wrap_socket(self, sock, server_hostname):
            hosts.append(server_hostname)
            return sock

    class Response:
        status = 302 if case == "redirect" else 403 if case == "unavailable" else 200
        offset = 0
        def getheader(self, key, default=""):
            return {"content-type": "text/html" if case == "not-json" else "application/json;charset=utf-8",
                    "content-encoding": "gzip" if case == "gzip" else "identity",
                    "age": "61" if case == "old-cache" else "0",
                    "location": "http://127.0.0.1/private"}.get(key, default)
        def read1(self, limit):
            assert 0 < limit <= 65536
            chunk = payload[self.offset:self.offset+limit]
            self.offset += len(chunk)
            return chunk

    class Connection:
        def __init__(self, host, port, timeout): pass
        def close(self): pass
        def request(self, method, target, headers):
            assert headers["Host"] == "site.api.espn.com"
            assert "MeshPotato" in headers["User-Agent"]
            assert headers["Accept-Encoding"] == "identity"
            assert headers["Cache-Control"] == "no-cache"
        def getresponse(self): return Response()

    def connect(address, timeout):
        addresses.append(address)
        return Sock()
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: [(2, 1, 6, "", ("8.8.8.8", 443))])
    monkeypatch.setattr(socket, "create_connection", connect)
    monkeypatch.setattr(web_sources.ssl, "create_default_context", lambda **kw: Context())
    monkeypatch.setattr(web_sources.http.client, "HTTPConnection", Connection)
    result = web_sources.fetch_page("https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard", json_response=True)
    assert ("events" in result) is usable
    assert addresses == [("8.8.8.8", 443)] and hosts == ["site.api.espn.com"]
    if not usable:
        assert result["_error"]


async def test_sports_timeout_never_counts_as_model_failure(harness):
    import asyncio
    h = harness(web_enabled=True, model_timeout_s=.02)
    async def slow(query):
        await asyncio.sleep(1)
    h.service.web.search = AsyncMock(side_effect=slow)
    assert await h.say(f"Michael: {QUERY}") is Decision.ANSWERED
    assert not h.backend.calls and h.service._backend_failures == 0
    assert h.sent == [(1, "@[Michael] " + sports.UNAVAILABLE)]


def test_tui_displays_lookup_diagnostics():
    from types import SimpleNamespace
    from bot.tui import MeshPotatoApp
    lines = []
    fake = SimpleNamespace(query_one=lambda *a: SimpleNamespace(write=lines.append))
    for name in ("sports_lookup", "web_lookup", "web_source_rejected", "web_retry", "web_answer", "generation_budget_exhausted"):
        MeshPotatoApp._on_record(fake, {"event": name, "ts": "2026-09-13T22:16:18Z", "outcome": "unverified"})
    assert len(lines) == 6 and all("unverified" in line for line in lines)


def test_dated_start_time_obeys_dst_rules():
    answer, evidence = render(event("pre", "STATUS_SCHEDULED", 0, day="2026-11-15"), query="Packers score 2026-11-15")
    assert evidence and "scheduled 14:25 CST" in answer


def test_local_clock_keeps_configured_timezone_rules(monkeypatch):
    monkeypatch.setenv("TZ", "America/Chicago")
    assert sports.local_now().tzinfo.key == "America/Chicago"


def test_nhl_shootout_is_not_reported_as_overtime():
    answer, evidence = render(event("in", "STATUS_SHOOTOUT", 5), league="nhl")
    assert evidence and "; shootout," in answer
