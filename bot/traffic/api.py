"""Bounded read-only access to the official Wisconsin 511 v2 API."""
import asyncio
import json
import logging
import math
import os
import re

import httpx

BASE = 'https://511wi.gov/api/v2/get/'


class FeedError(RuntimeError):
    """Deliberately contains no request URL, credentials or response body."""


class _RedactKey(logging.Filter):
    def filter(self, record):
        # httpx logs complete query URLs at INFO. Also protect diagnostic traces.
        message = record.getMessage()
        if '511wi.gov' in message or re.search(r'[?&]key=', message, re.I):
            record.msg = re.sub(r'([?&]key=)[^\s&\"\'<>]+', r'\1[REDACTED]', message, flags=re.I)
            record.args = ()
            record.exc_info = record.exc_text = record.stack_info = None
        return True


_redactor = _RedactKey()
logging.getLogger('httpx').addFilter(_redactor)


class Wisconsin511:
    def __init__(self, *, key=None, transport=None):
        self._key = key if key is not None else os.environ.get('WI511_API_KEY', os.environ.get('511_API_KEY', ''))
        self._transport = transport

    def validate(self):
        if not self._key.strip():
            raise FeedError('Traffic channel requires WI511_API_KEY in the process environment.')

    async def snapshot(self):
        self.validate()
        async with asyncio.timeout(15):
            async with httpx.AsyncClient(timeout=10, follow_redirects=False, trust_env=False,
                                         transport=self._transport) as client:
                results = await asyncio.gather(*(self._get(client, name) for name in ('event', 'alerts', 'winterroads')),
                                               return_exceptions=True)
        # Do not let a partial feed make a disappeared event look cleared.
        if any(isinstance(value, BaseException) for value in results):
            raise FeedError('511 feed unavailable or incomplete; retry at the next poll.')
        return dict(zip(('event', 'alerts', 'winterroads'), results))

    async def _get(self, client, endpoint):
        try:
            async with client.stream('GET', BASE+endpoint, params={'key': self._key, 'format': 'json'},
                                     headers={'Cache-Control': 'no-cache'}) as response:
                if response.status_code != 200:
                    raise FeedError(f'511 {endpoint}: HTTP {response.status_code}')
                if 'json' not in response.headers.get('content-type', '').lower():
                    raise FeedError('511 did not return JSON.')
                age = float(response.headers.get('age', '0'))
                if not math.isfinite(age) or not 0 <= age <= 60:
                    raise FeedError('511 returned an old HTTP cache.')
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > 8_000_000:
                        raise FeedError('511 response exceeds the size limit.')
                rows = json.loads(data)
                if not isinstance(rows, list) or len(rows) > 10000 or any(not isinstance(row, dict) for row in rows):
                    raise FeedError('511 returned an invalid feed.')
                return rows
        except asyncio.CancelledError:
            raise
        except FeedError:
            raise
        except Exception:
            raise FeedError('511 request failed; credentials and response details suppressed.') from None
