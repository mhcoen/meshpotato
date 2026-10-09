"""Background, model-free Madison corridor travel times from the public 511 table.

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
I90_ROUTES = {
    'I-39/90 NB US 12/18 to Badger Interchange': 'I90:NB',
    'I-39/90 SB Badger Interchange to US 12/18': 'I90:SB',
}


def is_traffic_question(prompt: str) -> bool:
    if re.search(r'\b(?:radio|mesh|network|packet|website|internet|air traffic)\b', prompt, re.I):
        return False
    return bool(re.search(r'\btraffic\b', prompt, re.I) or
                re.search(r'\b(?:crash|accident|closure|closed|icy|ice|delay|delays)\b', prompt, re.I)
                and re.search(r'\b(?:road|highway|belt\s*line|i[- ]?\d+|wis\s*\d+|us[- ]?\d+)\b', prompt, re.I))


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


def cached_traffic_direction(prompt: str, location: str = 'Madison, Wisconsin') -> str | None:
    beltline = beltline_direction(prompt, location)
    if beltline is not None:
        return beltline
    text = prompt.lower().replace('’', "'")
    text = re.sub(r'\bi\s*-?\s*(?:39\s*/\s*)?90\b|\binterstate\s+90\b', 'i90', text)
    words = set(re.findall(r'[a-z0-9]+', text))
    allowed = set(('what whats s is the how traffic on i90 madison wi wisconsin '
                   'in near around right now currently today please tell me about any delays '
                   'delay travel time times looking look like does it slow congested '
                   'congestion north south northbound southbound nb sb and both directions '
                   'can could you give a an update report').split())
    if ('i90' not in words or words - allowed
            or not words.intersection({'traffic', 'delay', 'delays', 'time', 'times', 'slow', 'congested', 'congestion'})
            or ('madison' not in words and not re.search(r'\bmadison\b', location, re.I))):
        return None
    north, south = bool(words & {'north', 'northbound', 'nb'}), bool(words & {'south', 'southbound', 'sb'})
    return 'I90:NB' if north and not south else 'I90:SB' if south and not north else 'I90'


def traffic_clarification(prompt: str, location: str) -> str:
    """Clarify a broad current-traffic request without claiming a feed outage."""
    words = set(re.findall(r'[a-z0-9]+', prompt.lower().replace('’', "'")))
    allowed = set(('what whats s is the how traffic downtown madison wi wisconsin '
                   'in around right now currently today please tell me about looking look like '
                   'can could you give a an update report').split())
    if 'traffic' not in words or words - allowed:
        return ''
    if 'downtown' in words:
        place = 'Downtown Madison' if 'madison' in words or re.search(r'\bmadison\b', location, re.I) else 'Downtown'
        return f'{place}: which street and direction? I can check reports, but have no downtown-wide live traffic feed.'
    return 'Which road and direction? I have cached 511 times for the Madison Beltline and I-90 between the Beltline and I-94.'


def traffic_followup(prompt: str) -> str | None:
    """A short road answer immediately after our clarification, never a new topic."""
    text = prompt.strip().rstrip('.?!')
    if (not 2 <= len(text) <= 80 or not re.fullmatch(r"[\w\s./'-]+", text)
            or re.search(r'\b(?:what|why|how|weather|sports|write|tell|explain|joke|poem|you|bot)\b', text, re.I)):
        return None
    if re.search(r'\b(?:street|st|road|rd|avenue|ave|drive|dr|hwy|highway|beltline|interstate|'
                 r'i\s*-?\s*90|john nolen|east wash(?:ington)?)\b', text, re.I):
        return 'traffic on ' + text
    return None


def _directions(direction: str) -> tuple[str, ...]:
    return ('I90:NB', 'I90:SB') if direction == 'I90' else (direction,) if direction else ('EB', 'WB')


@dataclass(frozen=True)
class Report:
    travel_minutes: float
    delay_minutes: float
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
    if (not isinstance(rows, list) or not rows or len(rows) > 200
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
        direction = I90_ROUTES.get(route.strip())
        if match:
            direction = 'EB' if match[1].startswith('EB') else 'WB'
        if direction is None:
            continue
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
        selected[direction] = Report(current, delay, observed, now, mono)
    return selected


async def _fetch_table(search: str) -> dict:
    """Same read-only request as 511's public travel-time table, without credentials."""
    columns = ('county', 'filterAndOrderProperty2', 'filterAndOrderProperty1',
               'distance', 'travelTimeNominal', 'travelTime', 'delay')
    query = {'start': 0, 'length': 100, 'search': {'value': search},
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


async def fetch_table() -> dict:
    """Fetch two bounded, fixed searches; one failing feed must not erase the other."""
    results = await asyncio.gather(_fetch_table('Beltline'), _fetch_table('I-39/90'), return_exceptions=True)
    rows = {}
    for result in results:
        if not isinstance(result, dict):
            continue
        data = result.get('data')
        if (not isinstance(data, list) or not 0 < len(data) <= 100
                or result.get('recordsFiltered') != len(data)
                or any(not isinstance(row, dict) or not isinstance(row.get('routeName'), str) for row in data)):
            continue
        # Search results may overlap. An exact duplicate is one measurement;
        # conflicting rows must fail rather than silently selecting a value.
        seen = set()
        for row in data:
            key = (row.get('county'), row['routeName'])
            if key in seen:
                raise ValueError('duplicate corridor rows')
            seen.add(key)
            if key in rows and rows[key] != row:
                raise ValueError('conflicting corridor rows')
            rows[key] = row
    if not rows:
        raise ValueError('no complete travel-time feeds')
    return {'recordsFiltered': len(rows), 'data': list(rows.values())}


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

    def _usable_reports(self, direction: str) -> dict[str, Report]:
        now, mono = self.wall_clock(), self.clock()
        return {d: r for d in _directions(direction)
                if (r := self.reports.get(d)) is not None and now >= r.observed_at
                and now >= r.fetched_at and mono >= r.fetched_mono}

    def _fresh(self, report: Report) -> bool:
        now, mono = self.wall_clock(), self.clock()
        return (0 <= now - report.observed_at < MAX_AGE_S
                and 0 <= now - report.fetched_at < MAX_AGE_S
                and 0 <= mono - report.fetched_mono < MAX_AGE_S)

    def is_current(self, direction: str) -> bool:
        reports = self._usable_reports(direction)
        return len(reports) == len(_directions(direction)) and all(self._fresh(r) for r in reports.values())

    def answer(self, direction: str, available: int, *, include_source: bool = True) -> str:
        unavailable = (TRAFFIC_UNAVAILABLE if len(TRAFFIC_UNAVAILABLE) <= available
                       else "I can get live traffic, but can't right now.")
        reports = self._usable_reports(direction)
        if not reports:
            return unavailable
        now = self.wall_clock()
        today = datetime.fromtimestamp(now, CENTRAL).date()
        parts = []
        for d in _directions(direction):
            name = {'EB': 'Eastbound', 'WB': 'Westbound', 'I90:NB': 'Northbound', 'I90:SB': 'Southbound'}[d]
            report = reports.get(d)
            if report is None:
                parts.append(f'{name} unavailable')
                continue
            if self._fresh(report):
                stamp = f'{int((now-report.observed_at)//60)} min ago'
            else:
                observed = datetime.fromtimestamp(report.observed_at, CENTRAL)
                days_ago = (today - observed.date()).days
                date = ('yesterday ' if days_ago == 1 else
                        observed.strftime('%m/%d/%Y ' if observed.year != today.year else '%m/%d ')
                        if days_ago else '')
                stamp = date + observed.strftime('%I:%M %p').lstrip('0')
            delay = 'no delay' if report.delay_minutes == 0 else f'{_number(report.delay_minutes)} min delay'
            parts.append(f'{name} {_number(report.travel_minutes)} min, {delay} ({stamp})')
        label = 'I-90 Beltline to I-94: ' if direction.startswith('I90') else 'Beltline: '
        answer = label + '. '.join(parts) + ('. Source: 511.' if include_source else '.')
        if len(answer) > available and direction == 'I90':
            notice = 'I-90 report needs one direction to fit: try /traffic I90 northbound or /traffic I90 southbound.'
            return notice if len(notice) <= available else unavailable
        return answer if len(answer) <= available else unavailable
