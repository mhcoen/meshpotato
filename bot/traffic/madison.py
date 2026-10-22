"""Bounded, model-free reader for official Madison transportation notices.

The old ArcGIS Road_Closures layer is archival (latest data observed: 2021).
Use dated city articles instead. Unknown schedules are omitted, never invented.
"""
import asyncio
import calendar
import math
from datetime import datetime, timedelta
import re
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
import httpx

from bot.reply import plain_ascii
from bot.traffic import CENTRAL
from bot.traffic.api import FeedError
from bot.traffic.planned import DAY, Notice, normalize, segment_key

ORIGIN = 'https://www.cityofmadison.com'
LIST_URL = ORIGIN+'/transportation/news'
MAX_ARTICLES = 60
MONTHS = {name.lower(): i for i in range(1, 13) for name in (calendar.month_name[i], calendar.month_abbr[i])}
MONTH = r'(?:January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)\.?'
DATE = re.compile(r'\b('+MONTH+r')\s+(\d{1,2})(?:st|nd|rd|th)?(?:\s*[-–]\s*(\d{1,2}))?(?:,?\s+(20\d{2}))?\b', re.I)
TIME = re.compile(r'\b(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)\b', re.I)
ROAD_WORDS = re.compile(r'\b(?:clos(?:e[ds]?|ures?|ing)|restrictions?|traffic pattern|reopen(?:ed|ing)?|reinstated|postponed|cancelled|canceled)\b', re.I)
# Intentional narrow scope. Side-street notices remain available on request.
MAJOR = re.compile(r'\b(?:downtown|capitol square|john nolen|washington|university|park (?:st|street)|regent|gorham|johnson|blair|broom|doty|wilson|dayton|bassett|bedford|henry|carroll|hamilton|pinckney|fairchild|gilman|langdon|wisconsin (?:ave|avenue)|state (?:st|street)|monona|atwood|williamson|eastwood|mineral point|fish hatchery|midvale|whitney|sherman|stoughton|highland|ironman|marathon|homecoming)\b', re.I)


def official_url(value):
    url = urlsplit(urljoin(ORIGIN, value))
    if (url.scheme != 'https' or url.hostname not in {'www.cityofmadison.com', 'www.madison.gov'}
            or url.username or url.password or url.port not in (None, 443) or not url.path.startswith('/news/')):
        return None
    return ORIGIN+url.path.rstrip('/')


def compact(text):
    text = plain_ascii(text)
    for long, short in [('Street', 'St'), ('Avenue', 'Ave'), ('Road', 'Rd'), ('Drive', 'Dr'),
                        ('Boulevard', 'Blvd'), ('North', 'N'), ('South', 'S'), ('East', 'E'), ('West', 'W')]:
        text = re.sub(r'\b'+long+r's?\b', short, text, flags=re.I)
    return ' '.join(text.split()).strip(' .,:;-')


def schedule(text, published):
    """Date-only and multi-phase notices stay date-only/multi-phase in the output."""
    ref = datetime.fromtimestamp(published, CENTRAL)
    dates = []
    for match in DATE.finditer(text):
        month = MONTHS.get(match[1].lower().rstrip('.').replace('sept', 'sep'))
        year = int(match[4]) if match[4] else ref.year
        if not match[4] and month < ref.month-6:
            year += 1
        try:
            for day in (match[2], match[3]):
                if day:
                    dates.append(datetime(year, month, int(day), tzinfo=CENTRAL))
        except ValueError:
            return None
    if not dates:
        return None
    unique = sorted(set(dates))
    start, last = unique[0], unique[-1]
    # Month-only reopening estimates are preserved as estimates, never exact days.
    estimated = re.search(r'\b(?:until|through)\s+(?:approximately\s+)?('+MONTH+r')\s+(20\d{2})\b', text, re.I)
    estimate_label = ''
    duration = re.search(r'\b(?:for|take)\s+(approximately\s+|about\s+)?(one|two|three|four|five|six|seven|eight|nine|ten|\d+)\s+(days?|weeks?)\b', text, re.I)
    if duration and len(unique) == 1:
        words = ['zero', 'one', 'two', 'three', 'four', 'five', 'six', 'seven', 'eight', 'nine', 'ten']
        amount = int(duration[2]) if duration[2].isdigit() else words.index(duration[2].lower())
        if not 0 < amount <= 365:
            return None
        last = start+timedelta(days=amount*(7 if duration[3].lower().startswith('week') else 1))
    if estimated:
        month = MONTHS[estimated[1].lower().rstrip('.').replace('sept', 'sep')]
        last = datetime(int(estimated[2]), month, calendar.monthrange(int(estimated[2]), month)[1], tzinfo=CENTRAL)
        estimate_label = last.strftime('%b %Y')+' est.'
    if last < start:
        return None
    multi = bool(len(unique) > 2 or re.search(r'\b(?:phase\s*[12]|nightly|intermittent|daily|each|every)\b', text, re.I))
    phased = multi
    times = list(TIME.finditer(text))
    interval = re.search(r'\b(?:from|between)\s+'+TIME.pattern+r'.{0,100}?\b(?:to|until|and)\s+'+TIME.pattern, text, re.I)
    def closure_clock_context(position):
        before = re.split(r'[.!?]\s+', text[:position])[-1]
        after = re.split(r'[.!?]\s+(?=[A-Z])', text[position:], maxsplit=1)[0]
        context = before+after
        return (bool(re.search(r'\bclos(?:e[ds]?|ures?)\b|\brestrictions?\b', context, re.I))
                and not re.search(r'\b(?:parade|race|concert|event)\s+(?:begins?|starts?)\b', context, re.I))
    if interval and not closure_clock_context(interval.start()):
        interval = None
    single_start = (len(times) == 1 and closure_clock_context(times[0].start())
                    and bool(re.search(r'\b(?:beginning|starting|starts?|from)\b',
                             text[max(0, times[0].start()-80):times[0].start()], re.I)))
    multi = multi or bool(times and not interval and not single_start)
    precise = False
    end = last+timedelta(days=1)
    # Only interpret clocks for a single explicit closure interval.
    # Multiple phases/times need a qualified notice, not guessed assignments.
    if len(unique) <= 2 and len(times) == 2 and interval and not multi and not estimated and not duration:
        values = []
        for index, t in enumerate(times):
            hour, minute = int(t[1]), int(t[2] or 0)
            if not 1 <= hour <= 12 or minute > 59:
                return None
            values.append((start if index == 0 else last).replace(hour=hour % 12+(12 if t[3].lower().startswith('p') else 0), minute=minute))
        if values[1] <= values[0]:
            if len(unique) == 1:
                values[1] += timedelta(days=1)
            else:
                return None
        start, end = values
        precise = True
    if single_start and not multi:
        t = times[0]
        hour, minute = int(t[1]), int(t[2] or 0)
        if not 1 <= hour <= 12 or minute > 59:
            return None
        start = start.replace(hour=hour % 12+(12 if t[3].lower().startswith('p') else 0), minute=minute)
    fmt = lambda d: f'{d.month}/{d.day}'+(f'/{d.year}' if d.year != ref.year else '')
    timing = fmt(start)
    if precise:
        clock = lambda d: d.strftime('%I:%M%p').lstrip('0').replace(':00', '')
        timing += ' '+clock(start)+'-'+(fmt(end)+' ' if start.date() != end.date() else '')+clock(end)
    elif last.date() != start.date():
        timing += ' to '+(estimate_label or fmt(last))
    if single_start and not multi:
        t = times[0]
        timing += ' from '+t[1]+(':'+t[2] if t[2] and t[2] != '00' else '')+('AM' if t[3].lower().startswith('a') else 'PM')
    if multi:
        timing += ' (phases; times vary)' if phased else ' (see notice for times)'
    elif re.search(r'\b(?:approximately|expected|anticipated|tentative)\b', text, re.I) and not estimate_label:
        timing += ' (est.)'
    if len(unique) == 1 and not precise and not multi and not estimated and not duration and re.search(r'\b(?:begin|starting|until further notice|indefinite)', text, re.I):
        end = None
        timing += '; end unconfirmed'
    return start.timestamp(), end.timestamp() if end else None, timing, precise, multi


def parse_notice(html, url):
    """Return None for nontraffic/ambiguous articles; no date inference from URL."""
    url = official_url(url)
    if not url:
        return None
    soup = BeautifulSoup(html, 'html.parser')
    title_node = soup.select_one('h1.page-title')
    body_node = soup.select_one('.block-field-blocknodenewsbody .field.body')
    date_node = soup.select_one('.date-posted time[datetime]')
    if title_node is None or body_node is None or date_node is None:
        raise FeedError('Madison article structure changed.')
    title = plain_ascii(title_node.get_text(' ', strip=True))
    body = plain_ascii(body_node.get_text(' ', strip=True))
    if not ROAD_WORDS.search(title) or len(body) > 40000:
        return None
    try:
        date = datetime.fromisoformat(date_node['datetime'])
        if date.tzinfo is None:
            return None
        published = date.timestamp()
    except (ValueError, TypeError):
        return None
    status = 'scheduled'
    # A statement about an open lane or a cancelled event inside a longer article
    # cannot cancel the whole closure. Require an explicit headline or lead.
    lead = body.split('.', 1)[0]
    for pattern, value in [(r'\bcancell?ed\b', 'cancelled'), (r'\bpostponed\b', 'postponed'),
                           (r'\breopened\b', 'reopened')]:
        if re.search(pattern, title, re.I) or re.search(r'\b(?:the|this) (?:planned )?(?:street |road )?closure (?:is|was|has been) '+pattern, body, re.I):
            status = value
            break
    partial = re.search(r'\b(northbound|southbound|eastbound|westbound) (?:closure )?(?:is |has been )?cancell?ed[;,]\s*(northbound|southbound|eastbound|westbound) (?:traffic )?remains closed\b', body, re.I)
    if partial and partial[1].lower() == partial[2].lower():
        return None
    if status != 'scheduled' and not partial and re.search(r'\b(?:partial|partially|only|except)\b', title+' '+lead, re.I):
        return None  # Cannot turn a partial cancellation into an all-clear.
    if status == 'scheduled' and not partial and re.search(r'\b(?:cancell?ed|postponed|reopened)\b', body, re.I):
        return None  # An unclear status statement must not be emitted as a closure.
    previous_start = None
    schedule_body = body
    if re.search(r'\b(?:originally|previously)\b', body, re.I) and status == 'scheduled':
        parts = re.split(r'\bnow\b', body, maxsplit=1, flags=re.I)
        if len(parts) != 2:
            return None
        original = schedule(parts[0], published)
        if original is None:
            return None
        previous_start = original[0]
        schedule_body = 'Closure: '+parts[1]
    parsed = schedule(schedule_body, published)
    if parsed is None and status == 'scheduled' and not partial:
        return None
    start, end, timing, precise, multi = parsed or (None, None, '', False, False)
    label = re.sub(r'^(?:(?:updated?|reminder|reinstated|cancelled|canceled|postponed|reopened)\s*[: -]+)+', '', title, flags=re.I)
    label = re.sub(r'^(?:temporary\s+)?(?:(?:street|road|lane)\s+(?:and\s+lane\s+)?)?closures?\s*(?:of|for|on|[-:])?\s*', '', label, flags=re.I)
    label = re.sub(r'\b(?:closure|closures|reopened|cancelled|canceled|postponed)\b', '', label, flags=re.I)
    label = re.sub(r'\s+(?:begins?|starting|on (?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday))\b.*$', '', label, flags=re.I)
    label = re.split(r'\s*[-–]\s*(?='+MONTH+r')', label, maxsplit=1, flags=re.I)[0]
    label = compact(label)
    if not label:
        return None
    restriction = 'closure planned'
    if multi:
        restriction = 'restrictions planned'
    elif re.search(r'\b(?:lane closure|lane closures|lane restriction|traffic pattern)\b', title, re.I):
        restriction = 'lane restrictions planned'
    # A direction must never be lost merely because it appears only in the body.
    directions = set(re.findall(r'\b(northbound|southbound|eastbound|westbound)\b', body, re.I))
    closed_dirs = set(re.findall(r'\b(northbound|southbound|eastbound|westbound)\b[^.!?]{0,100}\b(?:will (?:be )?clos(?:e|ed)|closed)\b', body, re.I))
    closed_dirs.update(re.findall(r'\bclose\s+(northbound|southbound|eastbound|westbound)\b', body, re.I))
    if not multi and closed_dirs:
        restriction = '/'.join(sorted(d.lower() for d in closed_dirs))+' closure planned'
    elif directions and not multi and not partial and not re.search(r'\bboth directions\b', body, re.I):
        return None  # Directional scope is unclear; do not imply both directions.
    if partial:
        status = 'scheduled'
        restriction = partial[1].lower()+' cancelled; '+partial[2].lower()+' remains closed'
    significant = bool(MAJOR.search(title)) and restriction != 'lane restrictions planned'
    related = tuple(sorted({u for a in body_node.select('a[href]') if (u := official_url(a['href'])) and u != url}))[:50]
    # Store a source-independent segment only when both endpoints are explicit.
    segment = ''
    m = re.search(r'(?P<road>.+?)\s+(?:between|from)\s+(?P<a>.+?)\s+(?:and|to)\s+(?P<b>.+)$', label, re.I)
    if m:
        segment = segment_key(m['road'], m['a'], m['b'], restriction)
    schedule_detail = ' '.join(m[0] for m in re.finditer(DATE.pattern+'|'+TIME.pattern, body, re.I)) if multi else ''
    return Notice(url, published, normalize(label), label, start, end, timing, restriction,
                  status, precise, significant, related, segment, schedule_detail=schedule_detail, previous_start=previous_start, inherit_schedule=bool(partial and parsed is None), reinstated=bool(re.search(r'\breinstated\b', title, re.I)))


class MadisonNotices:
    def __init__(self, *, transport=None):
        self.transport = transport
        self.omitted = self.unavailable = 0

    async def _get(self, client, url):
        # Follow only same-official-site redirects; no arbitrary linked hosts.
        for _ in range(4):
            async with client.stream('GET', url) as r:
                if r.status_code in {301, 302, 307, 308}:
                    target = urljoin(url, r.headers.get('location', ''))
                    parts = urlsplit(target)
                    if (parts.scheme != 'https' or parts.hostname not in {'www.cityofmadison.com', 'www.madison.gov'}
                            or parts.username or parts.password or parts.port not in (None, 443)):
                        raise FeedError('Madison redirected outside the official site.')
                    url = target
                    continue
                if r.status_code != 200 or 'html' not in r.headers.get('content-type', '').lower():
                    raise FeedError('Madison notice unavailable.')
                age = float(r.headers.get('age', '0'))
                if not math.isfinite(age) or not 0 <= age <= 6*3600:
                    raise FeedError('Madison returned an old HTTP cache.')
                data = bytearray()
                async for chunk in r.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > 1_000_000:
                        raise FeedError('Madison response exceeds size limit.')
                return data.decode('utf-8')
        raise FeedError('Too many Madison redirects.')

    async def snapshot(self, tracked=()):
        try:
            async with asyncio.timeout(25):
                async with httpx.AsyncClient(timeout=8, trust_env=False, follow_redirects=False,
                                             transport=self.transport) as client:
                    html = await self._get(client, LIST_URL)
                    soup = BeautifulSoup(html, 'html.parser')
                    links = soup.select('.article-title a[href]')
                    if not links:
                        raise FeedError('Madison news listing structure changed.')
                    urls = {official_url(a['href']) for a in links if ROAD_WORDS.search(a.get_text(' ', strip=True))}
                    urls.update(official_url(u) for u in tracked)
                    urls.discard(None)
                    if len(urls) > MAX_ARTICLES:
                        raise FeedError('Madison active notice limit reached; refusing a partial snapshot.')
                    sem = asyncio.Semaphore(4)
                    async def get(url):
                        async with sem:
                            page = await self._get(client, url)
                            return parse_notice(page, url)
                    results = await asyncio.gather(*(get(url) for url in sorted(urls)), return_exceptions=True)
                    self.unavailable = sum(isinstance(n, Exception) for n in results)
                    self.omitted = sum(n is None for n in results)
                    if results and self.unavailable == len(results):
                        raise FeedError('No Madison articles could be verified.')
                    return [n for n in results if isinstance(n, Notice)]
        except asyncio.CancelledError:
            raise
        except FeedError:
            raise
        except Exception:
            raise FeedError('Madison notices could not be verified.') from None
