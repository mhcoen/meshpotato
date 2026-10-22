"""Durable decisions for published closure notices, independent of polling/radio I/O.

An observation, a publication, and an attempted broadcast are different things.
Only claim() advances the broadcast baseline; disappearance never clears an event.
"""
from dataclasses import asdict, dataclass, replace
from datetime import datetime
import hashlib
import json
import math
import re

from bot.storage import StateError
from bot.traffic import CENTRAL

DAY = 86400
TERMINAL = {'cancelled', 'postponed', 'reopened'}
STATUSES = TERMINAL | {'scheduled'}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def normalize(text):
    text = text.casefold().replace('&', ' and ')
    for pattern, word in [(r'\bstreets?\b', 'st'), (r'\broads?\b', 'rd'),
                          (r'\bavenues?\b', 'ave'), (r'\bdrive\b', 'dr'),
                          (r'\bboulevard\b', 'blvd'), (r'\bwi[s]?[ -](?=\d)', 'wis '),
                          (r'\bnear\b', 'at')]:
        text = re.sub(pattern, word, text)
    return ' '.join(re.sub(r'[^a-z0-9 ]', ' ', text).split())


@dataclass(frozen=True)
class Notice:
    source_id: str                 # canonical official article URL, not fetch timestamp
    published: float
    subject: str                   # stable subject, without "updated/cancelled" or dates
    location: str                  # human readable, safe to render without losing direction
    start: float | None
    end: float | None              # exclusive expiry; never proof of reopening
    timing: str                    # retains date-only/estimated/recurring qualifications
    restriction: str               # normalized restriction facts (not contractor names)
    status: str = 'scheduled'
    precise: bool = False
    significant: bool = True
    related: tuple[str, ...] = ()  # explicit links to earlier notices
    segment: str = ''              # populated only with explicit road/direction/endpoints
    source: str = 'madison'
    schedule_detail: str = ''
    previous_start: float | None = None
    inherit_schedule: bool = False
    reinstated: bool = False

    @property
    def facts(self):
        return {k: getattr(self, k) for k in ('location', 'start', 'end', 'timing', 'restriction', 'status', 'precise', 'schedule_detail')}

    @property
    def fingerprint(self):
        facts = self.facts
        for key in ('location', 'timing', 'restriction', 'schedule_detail'):
            facts[key] = normalize(facts[key])
        return digest(facts)

    @property
    def publication(self):
        return digest((self.source_id, self.published))

    def relevant(self, now):
        if self.status in TERMINAL:
            return self.published >= now-7*DAY
        return (self.start is not None and self.start <= now+180*DAY
                and (self.end is None and now < max(self.start, self.published)+30*DAY
                     or self.end is not None and self.end > now))

    def render(self, prefix, budget):
        action = {'cancelled': 'closure cancelled', 'postponed': 'closure postponed; new date unconfirmed',
                  'reopened': 'reopened', 'scheduled': self.restriction}[self.status]
        when = '' if self.status in TERMINAL else '; '+self.timing
        text = f'{prefix}{self.location}: {action}{when}'.rstrip('.')+'.'
        # Never truncate a location, direction, schedule or cancellation qualifier.
        return text if len(text.encode('ascii')) <= budget else None


def read_notice(data):
    try:
        n = Notice(**data)
        if (n.status not in STATUSES or not all(isinstance(getattr(n, k), str) and len(getattr(n, k)) <= 2000
                for k in ('source_id', 'subject', 'location', 'timing', 'restriction', 'segment', 'source', 'schedule_detail'))
                or not n.source_id or not n.subject or not n.location
                or any(not isinstance(t, (int, float)) or isinstance(t, bool) or not math.isfinite(t) or t <= 0
                       for t in (n.published, n.start, n.end) if t is not None)
                or n.published is None or type(n.precise) is not bool or type(n.significant) is not bool
                or type(n.inherit_schedule) is not bool or type(n.reinstated) is not bool
                or n.previous_start is not None and (type(n.previous_start) not in (int, float) or not math.isfinite(n.previous_start))
                or not isinstance(n.related, (list, tuple)) or len(n.related) > 50
                or any(not isinstance(v, str) or len(v) > 2000 for v in n.related)):
            raise ValueError('invalid notice')
        return replace(n, related=tuple(n.related))
    except (TypeError, ValueError) as exc:
        raise StateError('Invalid saved planned-closure notice; preserved for recovery.') from exc


def validate_history(history):
    if not isinstance(history, dict) or len(history) > 10000:
        raise StateError('Invalid planned-closure history; preserved for recovery.')
    for key, record in history.items():
        if not isinstance(key, str) or not isinstance(record, dict):
            raise StateError('Invalid planned-closure record.')
        read_notice(record.get('latest', {}))
        if (not isinstance(record.get('aliases'), list) or len(record['aliases']) > 200
                or any(not isinstance(a, str) for a in record['aliases'])
                or type(record.get('reminded')) is not bool
                or type(record.get('conflict', False)) is not bool):
            raise StateError('Invalid planned-closure aliases or reminder state.')
        for field in ('announced', 'baseline'):
            if record.get(field) is not None:
                read_notice(record[field])
        observations = record.get('observations', {})
        if (not isinstance(observations, dict) or len(observations) > 200
                or any(not isinstance(k, str) or not isinstance(v, dict)
                       or not isinstance(v.get('fingerprint'), str)
                       or type(v.get('order')) not in (int, float) or not math.isfinite(v['order'])
                       for k, v in observations.items())
                or type(record.get('order', 0)) not in (int, float) or not math.isfinite(record.get('order', 0))):
            raise StateError('Invalid planned-closure observation order.')
        if record.get('announced') is not None and (type(record.get('at')) not in (int, float)
                or not math.isfinite(record['at'])):
            raise StateError('Invalid planned-closure receipt time.')


def overlaps(a, b):
    if a.start is None or b.start is None:
        return False
    return max(a.start, b.start) < min(a.end or a.start+DAY, b.end or b.start+DAY)


def same_event(n, old, aliases):
    linked = n.source_id in aliases or bool(set(n.related) & set(aliases))
    # Explicit links can connect a rescheduling; a reused URL after expiry cannot.
    if set(n.related) & set(aliases):
        return True
    if linked and n.start is not None and old.end is not None and n.start > old.end+7*DAY and n.previous_start is None:
        return False
    if linked:
        return (overlaps(n, old) or n.published <= (old.end or old.start or old.published)+7*DAY
                and (n.status in TERMINAL or old.status in TERMINAL or old.end is None or n.published < old.end))
    if normalize(n.subject) == normalize(old.subject):
        if n.previous_start is not None and old.start is not None and datetime.fromtimestamp(n.previous_start, CENTRAL).date() == datetime.fromtimestamp(old.start, CENTRAL).date():
            return True
        return overlaps(n, old) or ((n.status in TERMINAL or n.inherit_schedule) and n.start is None
                and n.published <= (old.end or old.start or 0)+7*DAY)
    # Cross-source matching requires a complete explicit segment and equal window.
    return bool(n.segment and n.segment == old.segment and n.start == old.start and n.end == old.end
                and normalize(n.restriction) == normalize(old.restriction))


def observe(history, notices, now, *, silent=False):
    """Merge one complete source snapshot into a copy of the caller's durable state."""
    active = set()
    for n in sorted(notices, key=lambda n: (n.published, n.source_id)):
        if n.published > now+60 or not (n.relevant(now) or n.inherit_schedule and n.published >= now-7*DAY):
            continue
        matches = [k for k, r in history.items() if same_event(n, read_notice(r['latest']), r['aliases'])]
        if len(matches) > 1:
            for key in matches:
                history[key]['conflict'] = True
                active.add(key)
            continue
        if n.status in TERMINAL and n.start is None and len(matches) == 1:
            old = read_notice(history[matches[0]]['latest'])
            n = replace(n, start=old.start, end=old.end, timing=old.timing, precise=old.precise,
                        restriction=old.restriction, segment=old.segment)
        if n.inherit_schedule:
            if len(matches) != 1:
                continue  # A scheduleless correction needs one previously verified event.
            old = read_notice(history[matches[0]]['latest'])
            n = replace(n, start=old.start, end=old.end, timing=old.timing, precise=old.precise, inherit_schedule=False)
        key = matches[0] if matches else digest((n.source_id, n.start, n.subject))
        if key not in history:
            if len(history) >= 10000:
                raise StateError('Planned-closure history is full; receipts preserved.')
            history[key] = dict(latest=asdict(n), aliases=[n.source_id], announced=None,
                                baseline=asdict(n) if silent else None, reminded=False, conflict=False, order=n.published,
                                observations={n.source_id: {'fingerprint': n.fingerprint, 'order': n.published}})
        else:
            r = history[key]
            old = read_notice(r['latest'])
            if n.source_id not in r['aliases']:
                if len(r['aliases']) >= 200:
                    raise StateError('Planned-closure alias history is full.')
                r['aliases'].append(n.source_id)
            if old.status in TERMINAL and n.status == 'scheduled' and n.published <= old.published and not n.reinstated:
                continue  # A cached pre-cancellation article is not reinstatement evidence.
            observations = r.setdefault('observations', {})
            previous = observations.get(n.source_id)
            order = (now if previous and previous['fingerprint'] != n.fingerprint
                     else max(n.published, previous['order'] if previous else n.published))
            observations[n.source_id] = {'fingerprint': n.fingerprint, 'order': order}
            if order < r.get('order', old.published):
                # An older article cannot keep an unavailable newer update fresh.
                continue
            if order == r.get('order', old.published) and n.source_id != old.source_id and n.fingerprint != old.fingerprint:
                r['conflict'] = True
            else:
                r['latest'] = asdict(n)
                r['order'] = order
                r['conflict'] = False
        active.add(key)
    return active


def decision(record, now):
    n = read_notice(record['latest'])
    if record.get('conflict'):
        return None, 'conflicting notices'
    if not n.relevant(now):
        return None, 'expired notice'
    old_data = record.get('announced')
    baseline = old_data or record.get('baseline')
    old = read_notice(baseline) if baseline else None
    if n.status in TERMINAL:
        if old_data is None:
            return None, 'original never announced'
        if n.fingerprint != old.fingerprint:
            return '', n.status
        return None, 'already announced'
    if not n.significant and old_data is None:
        return None, 'minor restriction'
    if old is None:
        return '', 'new notice'
    if old.status in TERMINAL:
        return 'Reinstated: ' if old.status == 'cancelled' else 'Updated: ', 'reinstated or rescheduled'
    if n.fingerprint != old.fingerprint:
        structural = (normalize(n.location) != normalize(old.location)
                      or normalize(n.restriction) != normalize(old.restriction) or n.precise != old.precise)
        deltas = [abs(a-b) for a, b in ((n.start, old.start), (n.end, old.end)) if a is not None and b is not None]
        dates_changed = any((a is None) != (b is None) for a, b in ((n.start, old.start), (n.end, old.end)))
        riskier = ((n.start is not None and old.start is not None and n.start < old.start)
                   or (n.end is not None and old.end is not None and n.end > old.end))
        # Small less restrictive shifts accumulate against the last broadcast, not the last poll.
        if structural or dates_changed or max(deltas, default=0) >= 30*60 or riskier and (n.start or now)-now <= DAY:
            return 'Updated: ' if old_data else '', 'material change'
        if normalize(n.schedule_detail) != normalize(old.schedule_detail) or not any(deltas) and normalize(n.timing) != normalize(old.timing):
            return 'Updated: ' if old_data else '', 'schedule details changed'
        return None, 'minor time change'
    if (old_data and not record['reminded'] and n.publication != old.publication
            and n.published > record['at'] and now-record['at'] >= 2*DAY
            and n.start is not None and 0 <= n.start-now <= DAY
            and 0 <= n.start-n.published <= DAY):
        return 'Reminder: ', 'fresh day-before notice'
    return None, 'already announced'


def claim(record, now, reason):
    old_data = record.get('announced')
    if old_data:
        old, new = read_notice(old_data), read_notice(record['latest'])
        if (old.status in TERMINAL and new.status == 'scheduled'
                or old.start and new.start and datetime.fromtimestamp(old.start, CENTRAL).date() != datetime.fromtimestamp(new.start, CENTRAL).date()):
            record['reminded'] = False
    record['announced'] = dict(record['latest'])
    record['at'] = now
    if reason == 'fresh day-before notice':
        record['reminded'] = True


@dataclass(frozen=True)
class PlannedAlert:
    key: str
    notice: Notice
    prefix: str
    reason: str
    priority: int = 3

    @property
    def identifier(self):
        return 'plan:'+self.key

    @property
    def fingerprint(self):
        return digest((self.notice.fingerprint, self.notice.publication, self.prefix))

    @property
    def rejection_key(self):
        return self.identifier, self.fingerprint

    def relevant(self, now, lookahead=0):
        return self.notice.relevant(now)

    def render(self, now, budget):
        return self.notice.render(self.prefix, budget)


def candidates(history, active, now):
    result = []
    for key in sorted(active):
        record = history[key]
        prefix, reason = decision(record, now)
        if prefix is not None:
            n = read_notice(record['latest'])
            result.append(PlannedAlert(key, n, prefix, reason, 0 if reason in TERMINAL or prefix == 'Updated: ' else 3))
    return result


def segment_key(road, first, last, restriction):
    """Exact named segment only; no proximity/geographic-radius matching."""
    road = normalize(road)
    directions = re.findall(r'\b(?:northbound|southbound|eastbound|westbound)\b', road+' '+restriction.lower())
    direction = ' '.join(sorted(set(directions)))
    road = re.sub(r'\b(?:northbound|southbound|eastbound|westbound)\b', '', road).strip()
    ends = [normalize(first), normalize(last)]
    # Headlines sometimes share a suffix: "N Charter and N Mills Streets".
    suffix = re.search(r'\b(st|rd|ave|dr|blvd)$', ends[1])
    if suffix and not re.search(r'\b(st|rd|ave|dr|blvd)$', ends[0]) and not re.search(r'\d', ends[0]):
        ends[0] += ' '+suffix[1]
    return '|'.join((road, direction, *ends))


def equivalent_511(n, alert):
    if (not alert.scheduled or not alert.full or n.status != 'scheduled' or not n.precise
            or n.start != alert.start or n.end != alert.end or not n.segment
            or n.restriction not in {'closure planned', 'northbound closure planned', 'southbound closure planned',
                                     'eastbound closure planned', 'westbound closure planned'}):
        return False
    endpoints = re.split(r'\s+to\s+', alert.place, flags=re.I)
    return (len(endpoints) == 2 and n.segment == segment_key(alert.road+' '+alert.direction,
                                                            *endpoints, n.restriction))


def inherit_511(record, alert, at):
    """Record equivalent facts already attempted on 511 without sending them again."""
    n = read_notice(record['latest'])
    if record.get('announced') is None and not record.get('conflict') and equivalent_511(n, alert):
        record['announced'] = asdict(replace(n, source_id='511:'+alert.identifier, published=at))
        record['at'] = at
