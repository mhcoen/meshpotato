"""Traffic-channel polling, durable announcements and model-free help."""
import asyncio
import copy
import math
import re
import time
from collections import OrderedDict

from bot.storage import StateError
from bot.channel_info import identity
from bot.traffic import cached_traffic_direction, traffic_clarification
from bot.traffic.alerts import FINGERPRINT_VERSION, HAZARD_PATTERNS, select_alerts

REANNOUNCE_S = 30*60


class TrafficChannel:
    def __init__(self, service, api, *, clock=time.monotonic, wall_clock=time.time):
        self.service, self.api = service, api
        self.clock, self.wall_clock = clock, wall_clock
        self.store = None
        self.state = {'version': 1, 'initialized': False, 'seen': {}}
        self.current = []
        self.rejected = set()
        self.cursors = OrderedDict()
        self.last_success = None
        self._task = None
        self.tick_s = 5.0
        self.lock = asyncio.Lock()
        self.counties = {name.strip().lower() for name in service.cfg.traffic_counties.split(',')}

    def prepare(self, store):
        self.api.validate()
        if store is None:
            raise StateError('Traffic announcements require persistent state.')
        self.store = store
        saved = store.load_traffic()
        if saved is not None:
            if (not isinstance(saved, dict) or saved.get('version') != 1
                    or type(saved.get('initialized')) is not bool or not isinstance(saved.get('seen'), dict)
                    or len(saved['seen']) > 10000):
                raise StateError('Invalid saved traffic announcement history; preserved for recovery.')
            for identifier, receipt in saved['seen'].items():
                if (not isinstance(identifier, str) or not isinstance(receipt, dict)
                        or not isinstance(receipt.get('fingerprint'), str)
                        or not re.fullmatch('[0-9a-f]{64}', receipt['fingerprint'])
                        or not isinstance(receipt.get('at'), (int, float)) or not math.isfinite(receipt['at'])):
                    raise StateError('Invalid saved traffic receipt; preserved for recovery.')
                scheme = receipt.get('fingerprint_version', 1)
                if type(scheme) is not int or scheme not in {1, FINGERPRINT_VERSION}:
                    raise StateError('Unsupported traffic fingerprint version; preserved for recovery.')
                if scheme == FINGERPRINT_VERSION:
                    lanes, hazards = receipt.get('blocked_lanes'), receipt.get('hazards')
                    if (type(receipt.get('full')) is not bool or 'blocked_lanes' not in receipt
                            or type(receipt.get('attempted', True)) is not bool
                            or lanes is not None and (type(lanes) is not int or lanes < 0)
                            or not isinstance(hazards, list) or any(not isinstance(h, str) or h not in HAZARD_PATTERNS for h in hazards)
                            or hazards != sorted(set(hazards))):
                        raise StateError('Invalid traffic fingerprint facts; preserved for recovery.')
            self.state = saved

    def start(self):
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name='traffic-announcements')

    async def stop(self):
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def _save(self, state):
        if len(state['seen']) > 10000:
            raise StateError('Traffic history capacity reached; existing receipts are preserved.')
        self.store.save_traffic(state)
        self.state = state

    @staticmethod
    def _receipt(alert, at, *, attempted=True):
        return {'fingerprint': alert.fingerprint, 'fingerprint_version': FINGERPRINT_VERSION,
                'at': at, 'full': alert.full, 'blocked_lanes': alert.blocked_lanes,
                'hazards': list(alert.hazards), 'attempted': attempted}

    async def refresh(self):
        async with self.lock:
            try:
                snapshot = await self.api.snapshot()
                current = select_alerts(snapshot, self.wall_clock(), self.counties,
                                        self.service.cfg.traffic_lookahead_h*3600)
                state = copy.deepcopy(self.state)
                # Baseline previously attempted identifiers once under the new scheme.
                # Preserve their actual claim times, including receipts for absent events.
                for alert in current:
                    previous = state['seen'].get(alert.identifier)
                    if previous is not None and previous.get('fingerprint_version', 1) == 1:
                        state['seen'][alert.identifier] = self._receipt(alert, previous['at'])
                if not state['initialized']:
                    state['initialized'] = True
                    if not self.service.cfg.traffic_announce_existing:
                        for alert in current:
                            state['seen'][alert.identifier] = self._receipt(alert, self.wall_clock(), attempted=False)
                if state != self.state:
                    self._save(state)
                self.current = current
                self.rejected.intersection_update(a.rejection_key for a in current)
                self.last_success = self.clock()
                self.service.log.emit('traffic_alert_refresh', outcome='ready', significant=len(current))
                return True
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.service.log.emit('traffic_alert_refresh', outcome='unavailable', reason=type(exc).__name__)
                return False

    def fresh(self):
        return self.last_success is not None and 0 <= self.clock()-self.last_success < 600

    def eligible(self, alert):
        return (self.fresh() and alert.relevant(self.wall_clock(), self.service.cfg.traffic_lookahead_h*3600)
                and alert.rejection_key not in self.rejected
                and self.state['seen'].get(alert.identifier, {}).get('fingerprint') != alert.fingerprint
                and self._repeat_allowed(alert)
                and any(a.identifier == alert.identifier and a.fingerprint == alert.fingerprint for a in self.current))

    def _repeat_allowed(self, alert):
        previous = self.state['seen'].get(alert.identifier)
        if previous is None:
            return True
        if previous.get('fingerprint_version', 1) != FINGERPRINT_VERSION:
            return False  # Refresh must commit migration before any announcement.
        return (not previous.get('attempted', True)
                or self.wall_clock()-previous['at'] >= REANNOUNCE_S
                or alert.full != previous['full']
                or list(alert.hazards) != previous['hazards']
                or (alert.blocked_lanes or 0) > (previous['blocked_lanes'] or 0))

    def _claim(self, alert):
        # Called synchronously by the radio send path after all checks, just before
        # its one attempt. An ambiguous failure/cancellation must never replay it.
        state = copy.deepcopy(self.state)
        state['seen'][alert.identifier] = self._receipt(alert, self.wall_clock())
        self._save(state)

    async def publish_pending(self):
        for alert in self.current:
            if not self.eligible(alert):
                continue
            text = alert.render(self.wall_clock(), min(self.service.cfg.reply_max_chars, self.service._reply_max_bytes))
            if not text:
                self.rejected.add(alert.rejection_key)
                self.service.log.emit('traffic_alert_skipped', reason='too-long', event_id=alert.identifier)
                continue
            attempted = False
            def claim():
                nonlocal attempted
                self._claim(alert)
                attempted = True
            try:
                outcome = await self.service.post_traffic_alert(text, claim, lambda: self.eligible(alert))
                self.service.log.emit('traffic_alert_post', event_id=alert.identifier, outcome=outcome, text=text)
            except StateError:
                self.service.log.emit('traffic_alert_post', event_id=alert.identifier, outcome='state-error')
                return
            if not attempted and outcome in {'blocked', 'send-failed'}:
                self.rejected.add(alert.rejection_key)
                continue
            # At most one announcement per tick. The shared radio limiter can
            # defer it longer; user requests get priority and no token is bypassed.
            return

    async def _run(self):
        next_poll = self.clock()
        while True:
            try:
                if self.clock() >= next_poll:
                    await self.refresh()
                    next_poll = self.clock()+self.service.cfg.traffic_refresh_s
                await self.publish_pending()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.service.log.emit('traffic_alert_error', reason=type(exc).__name__)
            await asyncio.sleep(self.tick_s)

    def answer(self, prompt, available, sender=''):
        q = ' '.join(prompt.lower().strip().lstrip('/!').rstrip('.?!').split())
        if q.startswith('web '):
            q = q[4:]
        if q in {'current alerts', 'what are the current alerts', 'what are the alerts',
                 'any alerts', 'any current alerts', "what's happening", 'what is happening',
                 'show alerts', 'show me the alerts', 'show me the current alerts', 'traffic alerts'}:
            q = 'alerts'
        if re.fullmatch(r'(?:belt\s*line|i[- ]?90)(?: (?:northbound|southbound|eastbound|westbound))?', q):
            q = 'traffic on '+q
        direction = cached_traffic_direction(q, self.service.cfg.web_location)
        if direction is not None:
            text = (self.service.traffic.answer(direction, available, include_source=False) if self.service.traffic is not None
                    else 'Travel-time lookup is disabled. Say alerts for significant 511 reports.')
        elif q in {'help', 'commands', 'what can you do', 'how does this work'}:
            text = 'Try traffic Beltline, traffic I90, current alerts, next, status, help policy or help coverage. / and ! work.'
        elif q == 'help policy':
            text = 'First start announces important current/upcoming reports. Later: new or changed reports only. Routine delays stay quiet.'
        elif q == 'help coverage':
            text = f'Alerts: {self.service.cfg.traffic_counties}; closures up to {self.service.cfg.traffic_lookahead_h:g}h ahead. Travel times: Madison Beltline and I90, Beltline to I94.'
        elif q in {'about', 'version', 'readme', 'help readme'}:
            name, version = identity('traffic')
            text = f'{name} v{version}: Alerts for {self.service.cfg.traffic_counties}; Madison travel times.'
            link = ' https://github.com/mhcoen/meshpotato/blob/main/bot/traffic/README.md'
            if len((text+link).encode()) <= available:
                text += link
        elif q == 'status':
            if self.last_success is None:
                text = 'Waiting for verified 511 data. Announcements are quiet until the feed is available.'
            else:
                age = max(0, int((self.clock()-self.last_success)//60))
                text = f'511 checked {age} min ago; {len(self.current)} significant reports. '+('Say alerts to read them.' if self.fresh() else 'Feed is stale; broadcasts paused.')
        elif q in {'next', 'next alert'} or re.fullmatch(r'(?:alerts|list|traffic)(?: \d{1,4})?', q):
            if not self.fresh():
                text = 'I can get live traffic, but cannot verify the alerts right now. Please try again shortly.'
            else:
                items = [a for a in self.current if a.relevant(self.wall_clock(), self.service.cfg.traffic_lookahead_h*3600)]
                signature = tuple(a.identifier+':'+a.fingerprint for a in items)
                previous_signature, previous_index = self.cursors.get(sender, ((), 0))
                if q in {'next', 'next alert'}:
                    index = previous_index+1 if previous_signature == signature else 1
                else:
                    index = int(q.split()[1]) if len(q.split()) == 2 else 1
                if not items:
                    text = 'No significant reports in the latest 511 feed for this area. This is not a guarantee that roads are clear.'
                elif not 1 <= index <= len(items):
                    text = f'Choose alerts 1 through alerts {len(items)}.'
                else:
                    prefix = f'{index} of {len(items)}. '
                    suffix = ' Say next.' if index < len(items) else ''
                    detail = items[index-1].render(self.wall_clock(), available-len(prefix)-len(suffix))
                    text = prefix+detail+suffix if detail else 'This report needs more room. See 511wi.gov for details.'
                    if detail:
                        self.cursors.pop(sender, None)
                        self.cursors[sender] = (signature, index)
                        while len(self.cursors) > 500:
                            self.cursors.popitem(last=False)
        else:
            text = traffic_clarification(q, self.service.cfg.web_location) or 'Try traffic Beltline, traffic I90, or alerts. Other road details: 511wi.gov. Say help for commands.'
        return text if len(text) <= available else 'Try help, status or alerts. Full traffic details: 511wi.gov.'
