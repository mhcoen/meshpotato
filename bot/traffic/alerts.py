"""Conservative selection and single-packet wording; never invent incident details."""
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import math
import re

from bot.reply import plain_ascii
from bot.traffic import CENTRAL

FINGERPRINT_VERSION = 2
HAZARD_PATTERNS = {
    'pileup': r'\bpile[ -]?ups?\b',
    'multiple vehicles': r'\b(?:multi[ -]?vehicle|multiple vehicles?)\b',
    'rollover': r'\broll[ -]?overs?\b',
    'jackknife': r'\bjackknif\w*\b',
    'vehicle fire': r'\bvehicle[ -]+fires?\b',
    'flood': r'\bflood\w*\b',
    'black ice': r'\bblack[ -]+ice\b',
    'ice': r'\b(?:ice|icy)\b',
    'hazmat': r'\b(?:hazmat|hazardous materials)\b',
    'impassable': r'\bimpassable\b',
    'travel not advised': r'\btravel not advised\b',
}


def hazard_facts(text):
    return tuple(sorted(name for name, pattern in HAZARD_PATTERNS.items() if re.search(pattern, text, re.I)))


def clean(value):
    return plain_ascii(value) if isinstance(value, str) else ''


def timestamp(value, *, optional=False):
    if value is None and optional:
        return None
    if isinstance(value, bool):
        raise ValueError('invalid time')
    number = float(value)
    if not math.isfinite(number) or not 0 < number < 4102444800:
        raise ValueError('invalid time')
    return number


def location(text):
    text = re.sub(r'\b(?:NB|SB|EB|WB)\b', '', text, flags=re.I)
    text = re.sub(r'\([^)]*\)', '', text)
    text = ' '.join(text.split()).strip(' .')
    text = re.sub(r'\b[A-Z]{2,}\b', lambda m: m[0].title(), text)
    return re.sub(r'\b(?:Wis|Sth|Us|Cth|I)\b', lambda m: {'Wis':'WIS','Sth':'WIS','Us':'US','Cth':'County','I':'I'}[m[0]], text)


def clock_text(stamp):
    return datetime.fromtimestamp(stamp, CENTRAL).strftime('%I:%M%p').lstrip('0').replace(':00', '')


@dataclass(frozen=True)
class Alert:
    identifier: str
    fingerprint: str
    priority: int
    road: str
    direction: str
    county: str
    kind: str
    detail: str
    place: str
    start: float
    end: float | None
    updated: float
    scheduled: bool = False
    full: bool = False
    blocked_lanes: int | None = None
    hazards: tuple[str, ...] = ()

    @property
    def rejection_key(self):
        # A wording correction may unblock the gate even when facts are unchanged.
        return self.identifier, hashlib.sha256((self.fingerprint+self.detail).encode()).hexdigest()

    def relevant(self, now, lookahead):
        return self.start <= now+lookahead and (self.end is None or self.end > now)

    def render(self, now, budget):
        road = ' '.join(filter(None, (self.road, self.direction)))
        when = ''
        if self.scheduled:
            if self.start > now:
                when = datetime.fromtimestamp(self.start, CENTRAL).strftime('%m/%d').lstrip('0')+' '+clock_text(self.start)
                if self.end and datetime.fromtimestamp(self.end, CENTRAL).date() == datetime.fromtimestamp(self.start, CENTRAL).date():
                    when += ' to '+clock_text(self.end)
                action = 'closed '+when
            else:
                action = 'closed'
        else:
            action = self.kind
        place = ': '+self.place if self.place else ''
        if self.scheduled and not self.place:
            # Some 511 rows say "WIS 19 to WIS 19". Do not present that as a
            # useful location or imply that the entire highway will close.
            timing = ', '+when if when else ''
            text = f'{road}: section closure in {self.county} County{timing}.'
            return text if len(text) <= budget else None
        candidates = [
            f'{road} {action}{place}.' if self.scheduled else self.detail,
            f'{road}: {action}{place}.',
            f'{road}, {self.county} County: {action}.',
        ]
        return next((line for line in candidates if line and len(line) <= budget), None)


def _make(identifier, priority, road, direction, county, kind, detail, place, start, end, updated, scheduled=False,
          *, full=False, blocked_lanes=None, hazards=()):
    # Render source wording, but identify changes by normalized facts only.
    fact_kind = 'winter hazard' if identifier.startswith('winter:') else kind
    material = (FINGERPRINT_VERSION, road.lower(), direction.lower(), county.lower(), fact_kind,
                full, blocked_lanes, hazards, place.lower(), start if scheduled else None, end, scheduled)
    fingerprint = hashlib.sha256(json.dumps(material, separators=(',', ':')).encode()).hexdigest()
    return Alert(identifier, fingerprint, priority, road, direction, county, kind, detail, place,
                 start, end, updated, scheduled, full, blocked_lanes, hazards)


def select_alerts(snapshot, now, counties, lookahead=86400):
    """One bad row is rejected, not converted into an invented warning."""
    selected = {}
    conflicting = set()
    for source, rows in snapshot.items():
        for row in rows:
            try:
                alert = _select(source, row, now, counties)
                if alert and alert.relevant(now, lookahead):
                    if alert.identifier in conflicting:
                        continue
                    previous = selected.get(alert.identifier)
                    if previous and previous.fingerprint != alert.fingerprint:
                        selected.pop(alert.identifier)
                        conflicting.add(alert.identifier)
                        continue
                    if previous and previous.updated >= alert.updated:
                        continue
                    selected[alert.identifier] = alert
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
    return sorted(selected.values(), key=lambda a: (a.priority, not bool(a.place), a.start, a.identifier))


def _select(source, row, now, counties):
    updated = timestamp(row['LastUpdated'])
    if updated > now+60:
        return None
    if source == 'event':
        county = clean(row['County'])
        if county.lower() not in counties:
            return None
        start, end = timestamp(row['StartDate']), timestamp(row.get('PlannedEndDate'), optional=True)
        road = clean(row['RoadwayName']).replace('WI-', 'WIS ').replace('US-', 'US ')
        direction = {'N':'northbound','S':'southbound','E':'eastbound','W':'westbound',
                     'North':'northbound','South':'southbound','East':'eastbound','West':'westbound',
                     'Both':'both directions','Both Directions':'both directions','All Directions':'both directions',
                     'Northbound':'northbound','Southbound':'southbound','Eastbound':'eastbound','Westbound':'westbound'}.get(row.get('DirectionOfTravel'), '')
        description = clean(row['Description'])
        category, subtype = row['EventType'], row.get('EventSubType', '')
        work = category == 'roadwork' or subtype == 'roadwork'
        full = row.get('IsFullClosure') is True
        lanes = clean(row.get('LanesAffected'))
        match = re.search(r'(\d+) lane(?:\(s\)|s)?(?: of \d+)? (?:blocked|closed)', lanes, re.I)
        blocked_lanes = int(match[1]) if match else None
        many_lanes = blocked_lanes is not None and blocked_lanes >= 2
        hazards = hazard_facts(description)
        hazardous = bool(hazards)
        congestion = re.sub(r'[^a-z]', '', str(subtype).lower()) in {'congestion', 'trafficcongestion', 'delay', 'delays'}
        if congestion and not (full or many_lanes or hazardous):
            return None  # A severity label alone does not make congestion broadcast worthy.
        if re.search(r'\b(?:cleared|all lanes (?:are )?open|reopened)\b', description, re.I):
            return None  # Feed disappearance/opening is not fabricated as an all-clear.
        if work:
            # Major-route through closures matter; routine works and ramp closures do not.
            if not full or not re.match(r'^(?:I-|US |WIS )\d', road) or re.search(r'\bramp\b', description, re.I):
                return None
            priority, kind = (3 if start > now else 2), 'closed'
        else:
            if category not in {'accidentsAndIncidents', 'closures'} or not (full or many_lanes or hazardous or str(row.get('Severity')).lower() in {'major', 'high', 'severe'}):
                return None
            priority, kind = 0, 'road closed' if full else 'serious incident'
        place_match = re.search(r'\bfrom (.+?)(?=\. (?:Daily|Detour|Expect)|$)', description, re.I)
        if not place_match:
            place_match = re.search(r'\b(?:near|at) (.+?)(?=\. |[,;]?\s+expect delays|$)', description, re.I)
        place = location(place_match[1]) if place_match else ''
        endpoints = re.split(r'\s+to\s+', place, flags=re.I)
        if len(endpoints) == 2 and endpoints[0].lower() == endpoints[1].lower():
            place = ''
        if not road or not description or end is not None and end <= start:
            return None
        sentences = {part.strip(' .!?').casefold() for part in re.split(r'(?<=[.!?])\s+', description)}
        detail = description
        if lanes and lanes.strip(' .!?').casefold() not in sentences:
            detail += ' '+lanes
        return _make('event:'+str(row['ID']), priority, road, direction, county, kind,
                     detail, place, start, end, updated, work,
                     full=full, blocked_lanes=blocked_lanes, hazards=hazards)
    if source == 'winterroads':
        county = clean(row['AreaName'])
        if county.lower() not in counties or now-updated > 3600:
            return None
        condition = clean(row['Overall Condition'])+' '+clean(row.get('RoadSurface'))
        if not re.search(r'\b(?:ice|icy|impassable|closed|travel not advised)\b', condition, re.I):
            return None
        if re.search(r'\b(?:no ice|ice.free|not icy)\b', condition, re.I):
            return None
        place = location(re.sub(r'\s*\([^)]*\)\s*$', '', clean(row['LocationDescription'])))
        road = clean(row['RoadwayName'])
        # Driving/passing lanes often duplicate one stretch. Announce that stretch once.
        identifier = 'winter:'+hashlib.sha256((county+'|'+place).lower().encode()).hexdigest()
        return _make(identifier, 1, road, '', county, condition.strip(),
                     f'{place}: {condition.strip()}.', place, 0, None, updated,
                     full=bool(re.search(r'\bclosed\b', condition, re.I)), hazards=hazard_facts(condition))
    if source == 'alerts':
        regions = row['Regions']
        if not isinstance(regions, list) or row.get('HighImportance') is not True:
            return None
        scope = {clean(region).lower() for region in regions}
        if not (scope & counties or scope & {'statewide', 'wisconsin', 'all regions'}
                or 'dane' in counties and 'southwest' in scope):
            return None
        start, end = timestamp(row['StartTime']), timestamp(row.get('EndTime'), optional=True)
        detail = clean(row['Message'])
        if not detail:
            return None
        return _make('alert:'+str(row['Id']), 0, '511 warning', '', ', '.join(sorted(scope)),
                     'high-importance alert', detail, '', start, end, updated,
                     full=bool(re.search(r'\b(?:roads? (?:is |are )?closed|all lanes closed)\b', detail, re.I)),
                     hazards=hazard_facts(detail))
    return None
