"""Bounded postseason schedules from labelled ESPN competitions, without a model."""
import concurrent.futures
import re
from datetime import timedelta

from bot.sports import (MAX_AGE_S, UNAVAILABLE, CLARIFY, _time, local_now,
                        parse_event, score_packet, score_answer)
from bot.sports_queries import series_name


def _dated_query(query, day):
    query = re.sub(r'\(as of .*?\)\s*$', '', query, flags=re.I)
    return f'{query} {day:%Y-%m-%d}'


def _candidate(page, query, now):
    if page.get('league') != 'mlb' or not -5 <= (now-_time(page['fetched_at'])).total_seconds() <= MAX_AGE_S:
        return None
    start = _time(page['event']['competitions'][0]['date']).astimezone(now.tzinfo)
    if not now.date() <= start.date() <= now.date()+timedelta(days=14):
        return None
    event = parse_event(page['event'], 'mlb', _dated_query(query, start), now)
    if not event or event['state'] != 'pre' or not event['status'].startswith('scheduled ') or start < now:
        return None
    return start


def collect_series(query, fetch, now=None):
    now = now or local_now()
    if series_name(query) == 'LCS':
        return [{'sports_clarify': True}]
    def daily(offset):
        day = now.date()+timedelta(days=offset)
        data = fetch(f'https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/scoreboard?dates={day:%Y%m%d}&limit=100')
        if (not isinstance(data.get('events'), list) or len(data['events']) >= 100
                or not any(l.get('slug') == 'mlb' for l in data.get('leagues', []))):
            raise ValueError('incomplete postseason scoreboard')
        return data['events']
    # Include the previous provider day for timezone boundaries. The existing
    # killable web worker and total generation deadline also bound these calls.
    for offset in range(-1, 16, 3):
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
                groups = list(pool.map(daily, range(offset, min(offset+3, 16))))
            found = {}
            for raw in (e for group in groups for e in group):
                raw_page = {'league': 'mlb', 'event': raw, 'fetched_at': now.isoformat()}
                if _candidate(raw_page, query, now):
                    packet = score_packet(raw, 'mlb', now)
                    key = raw['id']
                    if key in found and found[key] != packet:
                        raise ValueError('conflicting postseason snapshots')
                    found[key] = packet
            if found:
                earliest = min(_candidate(p, query, now) for p in found.values())
                return [p for p in found.values() if _candidate(p, query, now) == earliest][:3]
        except (KeyError, TypeError, ValueError, IndexError, AttributeError, OSError):
            return [{'sports_error': 'postseason schedule unavailable or incomplete'}]
    return []


def series_answer(pages, query, now, available):
    if series_name(query) == 'LCS':
        return 'Which championship series: ALCS or NLCS?', None
    if any('sports_error' in p for p in pages):
        return UNAVAILABLE, None
    found = {}
    for page in pages:
        try:
            start = _candidate(page, query, now)
            if start:
                key = page['event']['id']
                if key in found and found[key][1] != page:
                    return UNAVAILABLE, None
                found[key] = (start, page)
        except (KeyError, TypeError, ValueError, IndexError, AttributeError):
            continue
    if not found:
        return UNAVAILABLE, None
    earliest = min(start for start, _ in found.values())
    choices = [page for start, page in found.values() if start == earliest]
    if len(choices) != 1:
        return CLARIFY, None
    prefix = series_name(query)+': '
    text, evidence = score_answer(choices, _dated_query(query, earliest), now, available-len(prefix))
    return (prefix+text, evidence) if evidence else (UNAVAILABLE, None)
