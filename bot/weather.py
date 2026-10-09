"""Typed Open-Meteo daily summaries; no model-generated weather facts.

Provider contracts: https://open-meteo.com/en/docs and /en/docs/geocoding-api.
The disposable web worker fetches these pages under the shared lookup budget.
"""
from datetime import datetime, timedelta, timezone
import math
import re
from urllib.parse import urlencode

from bot.reply import plain_ascii
from bot.lookup_messages import WEATHER_UNAVAILABLE as UNAVAILABLE

CLARIFY = "Which city and state or country do you mean?"
MAX_AGE_S = 900
STATES = dict(zip(
    ('Alabama|Alaska|Arizona|Arkansas|California|Colorado|Connecticut|Delaware|Florida|Georgia|Hawaii|Idaho|Illinois|Indiana|Iowa|Kansas|Kentucky|Louisiana|Maine|Maryland|Massachusetts|Michigan|Minnesota|Mississippi|Missouri|Montana|Nebraska|Nevada|New Hampshire|New Jersey|New Mexico|New York|North Carolina|North Dakota|Ohio|Oklahoma|Oregon|Pennsylvania|Rhode Island|South Carolina|South Dakota|Tennessee|Texas|Utah|Vermont|Virginia|Washington|West Virginia|Wisconsin|Wyoming|District of Columbia').split('|'),
    'AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC'.split()))


def weather_location(prompt, default=''):
    """Only ordinary today/tomorrow summaries; specific/historical questions use web."""
    text = re.sub(r'\s*\(as of \d{4}-\d{2}-\d{2}\)\s*$', '', prompt, flags=re.I).strip()
    text = re.sub(r'^/web\s+', '', text, flags=re.I).strip(' ?.！')
    if re.fullmatch(r'what is weather', text, re.I):
        return None
    match = re.fullmatch(
        r"(?:(?:what(?:'s| is)|how(?:'s| is)) (?:the )?|(?:give me|show me) (?:the )?)?"
        r"(?:(?:daily|current|today's|tomorrow's) )?(?:weather|wx|forecast)"
        r"(?: (?:today|tomorrow))?(?:\s+(?:(?:in|for|at)\s+)?(.+?))?", text, re.I)
    if not match:
        return None
    place = match[1] or default
    place = re.sub(r'\s+(?:today|tomorrow)$', '', place, flags=re.I).strip(' ,?')
    if re.search(r'\b(?:yesterday|last|next|week|month|year|was|will|why|how|and)\b|\d{4}-\d{2}', place, re.I):
        return None
    return place


def _number(value, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError('invalid weather value')
    return value


def collect_weather(query, fetch, now=None):
    now = now or datetime.now(timezone.utc)
    place = weather_location(query)
    if not place:
        return [{'weather_error': 'location'}]
    try:
        geo_url = 'https://geocoding-api.open-meteo.com/v1/search?' + urlencode(
            {'name': place, 'count': 10, 'language': 'en', 'format': 'json'})
        rows = fetch(geo_url).get('results', [])
        if not isinstance(rows, list) or not rows:
            return [{'weather_error': 'unavailable'}]
        # The provider applies comma qualifiers. Still verify them ourselves;
        # never silently choose another state or country with the same city name.
        parts = [p.strip().casefold() for p in place.split(',')]
        matches = []
        for row in rows:
            name = row.get('name', '')
            qualifiers = {str(row.get(k, '')).casefold() for k in ('admin1', 'country', 'country_code')}
            qualifiers.add(STATES.get(row.get('admin1', ''), '').casefold())
            if (plain_ascii(name).casefold() == plain_ascii(parts[0]).casefold()
                    or parts[0] in row.get('postcodes', [])) and all(p in qualifiers for p in parts[1:]):
                matches.append(row)
        if len(matches) != 1:
            return [{'weather_error': 'location'}]
        row = matches[0]
        lat, lon = _number(row['latitude'], -90, 90), _number(row['longitude'], -180, 180)
        region = STATES.get(row.get('admin1')) if row.get('country_code') == 'US' else row.get('country_code')
        label = plain_ascii(row['name'] + (', ' + region if region else ''))
        if not re.fullmatch(r"[\w .,'-]{1,70}", label, re.ASCII):
            raise ValueError('invalid location label')
        url = 'https://api.open-meteo.com/v1/forecast?' + urlencode({
            'latitude': lat, 'longitude': lon, 'timezone': 'auto', 'forecast_days': 2,
            'temperature_unit': 'fahrenheit', 'wind_speed_unit': 'mph',
            'current': 'temperature_2m,weather_code,wind_speed_10m,wind_direction_10m',
            'daily': 'weather_code,temperature_2m_max,temperature_2m_min'})
        return [{'weather': fetch(url), 'location': label, 'fetched_at': now.isoformat(),
                 'url': url, 'geocoding_url': geo_url}]
    except (KeyError, ValueError, TypeError, AttributeError):
        return [{'weather_error': 'unavailable'}]


def _condition(code):
    code = _number(code, 0, 99)
    conditions = {0: ('☀️', 'Clear'), 1: ('🌤️', 'Mainly clear'), 2: ('⛅', 'Partly cloudy'),
                  3: ('☁️', 'Overcast'), 45: ('🌫️', 'Fog'), 48: ('🌫️', 'Rime fog')}
    for codes, value in [((51, 53, 55), ('🌦️', 'Drizzle')), ((56, 57), ('🌧️', 'Freezing drizzle')),
                         ((61, 63, 65), ('🌧️', 'Rain')), ((66, 67), ('🌧️', 'Freezing rain')),
                         ((71, 73, 75, 77), ('❄️', 'Snow')), ((80, 81, 82), ('🌦️', 'Showers')),
                         ((85, 86), ('❄️', 'Snow showers')), ((95, 96, 99), ('⛈️', 'Thunderstorms'))]:
        conditions.update(dict.fromkeys(codes, value))
    if code not in conditions:
        raise ValueError('unknown weather code')
    return conditions[code]


def weather_answer(pages, now, available):
    """Return a complete, byte-fitting summary and provenance, or an honest notice."""
    try:
        page = pages[0]
        if page.get('weather_error') == 'location':
            return CLARIFY, None
        fetched = datetime.fromisoformat(page['fetched_at'])
        if not 0 <= (now - fetched).total_seconds() <= MAX_AGE_S:
            raise ValueError('stale fetch')
        if not page['url'].startswith('https://api.open-meteo.com/v1/forecast?'):
            raise ValueError('unexpected weather origin')
        data, label = page['weather'], page['location']
        if not re.fullmatch(r"[\w .,'-]{1,70}", label, re.ASCII):
            raise ValueError('invalid location')
        cu, du = data['current_units'], data['daily_units']
        if (cu['temperature_2m'] != '°F' or cu['wind_speed_10m'] != 'mp/h'
                or cu['wind_direction_10m'] != '°' or cu['weather_code'] != 'wmo code'
                or du['temperature_2m_max'] != '°F' or du['temperature_2m_min'] != '°F'
                or du['weather_code'] != 'wmo code'):
            raise ValueError('unexpected units')
        offset = _number(data['utc_offset_seconds'], -50400, 50400)
        tz = timezone(timedelta(seconds=offset))
        today = now.astimezone(tz).date()
        current, daily = data['current'], data['daily']
        observed = datetime.fromisoformat(current['time'])
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=tz)
        if not -300 <= (now - observed).total_seconds() <= 5400:
            raise ValueError('stale current conditions')
        expected = [today.isoformat(), (today + timedelta(days=1)).isoformat()]
        if daily['time'] != expected:
            raise ValueError('wrong forecast days')
        highs = [_number(v, -150, 150) for v in daily['temperature_2m_max']]
        lows = [_number(v, -150, 150) for v in daily['temperature_2m_min']]
        if len(highs) != 2 or len(lows) != 2 or any(lo > hi for hi, lo in zip(highs, lows)):
            raise ValueError('invalid daily temperatures')
        temp = round(_number(current['temperature_2m'], -150, 150))
        speed = round(_number(current['wind_speed_10m'], 0, 300))
        direction = _number(current['wind_direction_10m'], 0, 360)
        compass = 'N NNE NE ENE E ESE SE SSE S SSW SW WSW W WNW NW NNW'.split()[int((direction+11.25)//22.5)%16]
        icon, condition = _condition(current['weather_code'])
        _condition(daily['weather_code'][0])
        next_icon, next_condition = _condition(daily['weather_code'][1])
        evidence = {'url': page['url'], 'geocoding_url': page['geocoding_url'],
                    'fetched_at': page['fetched_at'], 'location': label,
                    'current_time': current['time'], 'forecast_dates': expected,
                    'valid_until': min(fetched + timedelta(seconds=MAX_AGE_S),
                                       observed + timedelta(seconds=5400),
                                       datetime.combine(today + timedelta(days=1), datetime.min.time(), tzinfo=tz)).isoformat()}
        # Prefer the requested presentation; drop decoration before information.
        for heading, tomorrow, icons, degree in [('🌤️ Daily Weather: ', 'Tomorrow', True, '°'),
                                                ('Weather: ', 'Tomorrow', True, '°'),
                                                ('', 'Tmr', False, '°'), ('', 'Tmr', False, '')]:
            text = (f'{heading}{label}: {icon if icons else ""}{condition} {temp}{degree}F {compass}{speed}mph'
                    f' | H:{round(highs[0])}{degree}F L:{round(lows[0])}{degree}F'
                    f' | {tomorrow}: {next_icon if icons else ""}{next_condition}'
                    f' H:{round(highs[1])}{degree}F L:{round(lows[1])}{degree}F')
            if len(text.encode('utf-8')) <= available:
                return text, evidence
        return 'The weather summary will not fit; please use a shorter sender name.', None
    except (KeyError, IndexError, TypeError, ValueError, OverflowError):
        return UNAVAILABLE, None
