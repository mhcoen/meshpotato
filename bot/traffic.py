"""Background, model-free Madison Beltline travel times from the public 511 table.

No radio work happens here. Only fresh observations and fetches are called current;
older measurements are shown with their original update times. Failed polls never
extend either timestamp.
"""
from __future__ import annotations

import asyncio
import json
import math
import re
import time
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

from bot.lookup_messages import TRAFFIC_UNAVAILABLE

SOURCE = 'https://511wi.gov/list/traveltimes'
ENDPOINT = 'https://511wi.gov/List/GetData/TravelTimes'
CENTRAL = ZoneInfo('America/Chicago')
MAX_AGE_S = 600.0


def beltline_direction(prompt: str, location: str = 'Madison, Wisconsin') -> str | None:
    """Only current travel-time questions about our known corridor, not other roads."""
    text = prompt.lower().replace('’', "'")
    text = re.sub(r'^\s*(?:hey\s+)?mesh\s*potato\s*[,!:]?\s*', '', text)
    text = re.sub(r'\bbelt\s+line\b', 'beltline', text)
    text = re.sub(r'\(?\s*\b(?:hwy\s*|highway\s*|us\s*)12\s*/\s*18\s*\)?', '', text)
    if 'beltline' not in text:
        return None
    # Keep forecasts, events, specific exits/segments, and mixed-topic requests on
    # the normal lookup path. A travel-time table cannot answer those questions.
    words = set(re.findall(r'[a-z0-9]+', text))
    allowed = set(('what whats s is the how traffic on beltline madison wi wisconsin '
                   'in right now currently today please tell me about any delays '
                   'delay travel time times looking look like does it slow congested '
                   'congestion east west eastbound westbound eb wb and both directions '
                   'can could you give a an update report').split())
    if words - allowed or not words.intersection({'traffic', 'delay', 'delays', 'time', 'times', 'slow', 'congested', 'congestion'}):
        return None
    if 'madison' not in words and not re.search(r'\bmadison\b', location, re.I):
        return None
    east = bool(words & {'east', 'eastbound', 'eb'})
    west = bool(words & {'west', 'westbound', 'wb'})
    return 'EB' if east and not west else 'WB' if west and not east else ''


@dataclass(frozen=True)
class Report:
    text: str
    observed_at: float
    fetched_at: float
    fetched_mono: float


def _minutes(value) -> float:
    if isinstance(value, bool):
        raise ValueError('invalid minutes')
    number = float(value)
    if not math.isfinite(number) or number < 0 or number > 1440:
        raise ValueError('invalid minutes')
    return number


def _number(value: float) -> str:
    return str(round(value, 1)).removesuffix('.0')


def parse_reports(payload: dict, now: float, mono: float) -> dict[str, Report]:
    """Validate the observed public-table schema; never infer clear roads from emptiness."""
    rows = payload.get('data') if isinstance(payload, dict) else None
    if (not isinstance(rows, list) or not rows or len(rows) > 100
            or payload.get('recordsFiltered') != len(rows)):
        raise ValueError('missing or incomplete travel-time table')
    selected = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('malformed row')
        route = row.get('routeName', '')
        if row.get('county') != 'Dane' or not isinstance(route, str):
            continue
        match = re.fullmatch(r'US 12 (EB University Ave to Beltline Interchange|WB Beltline Interchange to University Ave)', route.strip())
        if not match:
            continue
        direction = 'EB' if match[1].startswith('EB') else 'WB'
        try:
            current = _minutes(row['travelTime'])
            normal = _minutes(row['travelTimeNominal'])
            delay = _minutes(row['delay'])
            observed = datetime.strptime(row['lastUpdated'], '%m/%d/%y, %I:%M %p').replace(tzinfo=CENTRAL).timestamp()
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise ValueError('invalid travel-time measurement') from exc
        # Zero is 511's "not available" sentinel. Do not make it a zero-minute trip.
        if current == 0 or normal == 0 or observed > now:
            continue
        if abs(max(0, current - normal) - delay) > 1:
            raise ValueError('inconsistent travel-time measurement')
        if direction in selected:
            raise ValueError('duplicate corridor')
        selected[direction] = (f'{direction} {_number(current)}m (+{_number(delay)})', observed)
    reports = {d: Report(f'Beltline Univ to I-39/90: {text}' if d == 'EB' else f'Beltline I-39/90 to Univ: {text}', observed, now, mono)
               for d, (text, observed) in selected.items()}
    if len(selected) == 2:
        reports[''] = Report('Beltline Univ<>I-39/90: ' + '; '.join(selected[d][0] for d in ('EB', 'WB')),
                             min(value[1] for value in selected.values()), now, mono)
    return reports


async def fetch_table() -> dict:
    """Same read-only request as 511's public travel-time table, without credentials."""
    columns = ('county', 'filterAndOrderProperty2', 'filterAndOrderProperty1',
               'distance', 'travelTimeNominal', 'travelTime', 'delay')
    query = {'start': 0, 'length': 100, 'search': {'value': 'Beltline'},
             'order': [{'column': 1, 'dir': 'asc'}],
             'columns': [{'name': name, 's': True} for name in columns]}
    async with httpx.AsyncClient(timeout=10, follow_redirects=False, trust_env=False) as client:
        async with client.stream('GET', ENDPOINT, params={'query': json.dumps(query), 'lang': 'en'},
                                 headers={'X-Requested-With': 'XMLHttpRequest', 'Cache-Control': 'no-cache'}) as response:
            response.raise_for_status()
            if 'json' not in response.headers.get('content-type', '').lower():
                raise ValueError('not a JSON table')
            age = float(response.headers.get('age', '0'))
            if not math.isfinite(age) or not 0 <= age <= 60:
                raise ValueError('stale HTTP cache')
            data = bytearray()
            async for chunk in response.aiter_bytes():
                data.extend(chunk)
                if len(data) > 1_000_000:
                    raise ValueError('table too large')
            return json.loads(data)


class TrafficCache:
    def __init__(self, log, *, refresh_s=300.0, fetch=fetch_table,
                 clock=time.monotonic, wall_clock=time.time):
        self.log = log
        self.refresh_s = refresh_s
        self.fetch = fetch
        self.clock = clock
        self.wall_clock = wall_clock
        self.reports: dict[str, Report] = {}
        self._task = None
        self._lock = asyncio.Lock()

    def start(self):
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name='traffic-cache')

    async def stop(self):
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _run(self):
        while True:
            await self.refresh()
            await asyncio.sleep(self.refresh_s)

    async def refresh(self):
        async with self._lock:
            try:
                payload = await asyncio.wait_for(self.fetch(), timeout=12.0)
                reports = parse_reports(payload, self.wall_clock(), self.clock())
                if not reports:
                    raise ValueError('no valid corridor measurements')
            except Exception as exc:
                # Never log arbitrary response bodies/URLs or discard a last good report.
                self.log.emit('traffic_refresh', outcome='unavailable', reason=type(exc).__name__)
                return False
            self.reports.update(reports)
            self.log.emit('traffic_refresh', outcome='ready', directions=sorted(reports), source=SOURCE)
            return True

    def answer(self, direction: str, available: int) -> str:
        unavailable = (TRAFFIC_UNAVAILABLE if len(TRAFFIC_UNAVAILABLE) <= available
                       else "I can get live traffic, but can't right now.")
        now = self.wall_clock()
        mono = self.clock()
        wanted = (direction,) if direction else ('EB', 'WB')
        reports = {d: r for d in wanted if (r := self.reports.get(d)) is not None
                   and now >= r.observed_at and now >= r.fetched_at and mono >= r.fetched_mono}
        if not reports:
            return unavailable
        if len(reports) == len(wanted) and all(
            now - r.observed_at < MAX_AGE_S and now - r.fetched_at < MAX_AGE_S
            and mono - r.fetched_mono < MAX_AGE_S for r in reports.values()
        ):
            # Rebuild both directions from their latest individual reports, so a
            # partial poll cannot leave an older combined snapshot in the answer.
            text = (reports[direction].text if direction else 'Beltline Univ<>I-39/90: '
                    + '; '.join(reports[d].text.split(': ', 1)[1] for d in wanted))
            age = int((now - min(r.observed_at for r in reports.values())) // 60)
            answer = f'{text}. 511 {age}m ago; +delay.'
        else:
            today = datetime.fromtimestamp(now, CENTRAL).date()
            def stamp(report, compact=False):
                observed = datetime.fromtimestamp(report.observed_at, CENTRAL)
                date = (observed.strftime('%m/%d/%Y ' if observed.year != today.year else '%m/%d ')
                        if observed.date() != today else '')
                return date + observed.strftime('%I:%M%p' if compact else '%I:%M %p').lstrip('0')
            if len(reports) == 1:
                d, report = next(iter(reports.items()))
                zone = datetime.fromtimestamp(report.observed_at, CENTRAL).tzname()
                missing = f' {"WB" if d == "EB" else "EB"} unavailable.' if not direction else ''
                answer = f'Most recent update {stamp(report)} {zone}: {report.text}. 511; +delay.{missing}'
            else:
                # Each direction has its own source timestamp, including a date
                # for prior days. Do not apply the newer time to both directions.
                parts = []
                for d, report in reports.items():
                    measurement = report.text.split(': ', 1)[1].removeprefix(d + ' ')
                    zone = datetime.fromtimestamp(report.observed_at, CENTRAL).tzname()
                    parts.append(f'{d} {stamp(report, compact=True)} {zone} {measurement}')
                answer = 'Most recent 511 updates: ' + '; '.join(parts) + '. Beltline Univ<>I-39/90; +delay.'
        return answer if len(answer) <= available else unavailable
