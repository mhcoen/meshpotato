"""Traffic-channel polling, durable announcements and model-free help."""
import asyncio
import copy
from dataclasses import replace
import math
import re
import time
from collections import OrderedDict

from bot.storage import StateError
from bot.channel_info import identity
from bot.traffic import cached_traffic_direction, traffic_clarification
from bot.traffic.alerts import FINGERPRINT_VERSION, HAZARD_PATTERNS, select_alerts
from bot.traffic import planned

REANNOUNCE_S = 30*60


class TrafficChannel:
    def __init__(self, service, api, *, city_api=None, clock=time.monotonic, wall_clock=time.time):
        self.service, self.api, self.city_api = service, api, city_api
        self.city_success = None
        self.city_active = set()
        self.city_reasons = {}
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
                if type(scheme) is not int or scheme not in {1, 2, FINGERPRINT_VERSION}:
                    raise StateError('Unsupported traffic fingerprint version; preserved for recovery.')
                if scheme >= 2:
                    lanes, hazards = receipt.get('blocked_lanes'), receipt.get('hazards')
                    if (type(receipt.get('full')) is not bool or 'blocked_lanes' not in receipt
                            or type(receipt.get('attempted', True)) is not bool
                            or lanes is not None and (type(lanes) is not int or lanes < 0)
                            or not isinstance(hazards, list) or any(not isinstance(h, str) or h not in HAZARD_PATTERNS for h in hazards)
                            or hazards != sorted(set(hazards))):
                        raise StateError('Invalid traffic fingerprint facts; preserved for recovery.')
                if 'broadcast_key' in receipt and (not isinstance(receipt['broadcast_key'], str)
                        or not re.fullmatch('[0-9a-f]{64}', receipt['broadcast_key'])):
                    raise StateError('Invalid traffic broadcast receipt; preserved for recovery.')
            planned.validate_history(saved.get('plans', {}))
            if type(saved.get('plans_initialized', False)) is not bool:
                raise StateError('Invalid planned-closure initialization state.')
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
                'hazards': list(alert.hazards), 'attempted': attempted, 'broadcast_key': alert.broadcast_key}

    async def _refresh_511(self):
        async with self.lock:
            try:
                snapshot = await self.api.snapshot()
                current = select_alerts(snapshot, self.wall_clock(), self.counties,
                                        self.service.cfg.traffic_lookahead_h*3600, deduplicate=False)
                state = copy.deepcopy(self.state)
                # Baseline previously attempted identifiers once under the new scheme.
                # Preserve their actual claim times, including receipts for absent events.
                for alert in current:
                    previous = state['seen'].get(alert.identifier)
                    if previous is not None and previous.get('fingerprint_version', 1) != FINGERPRINT_VERSION:
                        state['seen'][alert.identifier] = self._receipt(alert, previous['at'], attempted=previous.get('attempted', True))
                if not state['initialized']:
                    state['initialized'] = True
                    if not self.service.cfg.traffic_announce_existing:
                        for alert in current:
                            state['seen'][alert.identifier] = self._receipt(alert, self.wall_clock(), attempted=False)
                if state != self.state:
                    self._save(state)
                # Migrate receipts for every source ID before merging aliases.
                unique = {}
                for alert in current:
                    unique.setdefault(alert.broadcast_key, alert)
                self.current = list(unique.values())
                self.rejected.intersection_update({a.rejection_key for a in current} | {k for k in self.rejected if k[0].startswith('plan:')})
                self.last_success = self.clock()
                self.service.log.emit('traffic_alert_refresh', outcome='ready', significant=len(self.current))
                return True
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.service.log.emit('traffic_alert_refresh', outcome='unavailable', reason=type(exc).__name__)
                return False

    async def refresh(self):
        if self.city_api is None:
            return await self._refresh_511()
        results = await asyncio.gather(self._refresh_511(), self._refresh_city())
        return any(results)

    def city_fresh(self):
        return self.city_success is not None and 0 <= self.clock()-self.city_success < 600

    async def _refresh_city(self):
        now = self.wall_clock()
        tracked = [r['latest']['source_id'] for r in self.state.get('plans', {}).values()
                   if planned.read_notice(r['latest']).source == 'madison'
                   and planned.read_notice(r['latest']).relevant(now)]
        try:
            notices = await self.city_api.snapshot(tracked)
            async with self.lock:
                state = copy.deepcopy(self.state)
                history = state.setdefault('plans', {})
                active = planned.observe(history, notices, now,
                    silent=not state.get('plans_initialized', False) and not self.service.cfg.traffic_announce_existing)
                state['plans_initialized'] = True
                for record in history.values():
                    for alert in self.current:
                        receipt = state['seen'].get(alert.identifier, {})
                        if receipt.get('attempted', True) and receipt.get('fingerprint') == alert.fingerprint:
                            planned.inherit_511(record, alert, receipt['at'])
                if state != self.state:
                    self._save(state)
                self.city_active = active
                valid_keys = {a.rejection_key for a in planned.candidates(history, active, now)}
                self.rejected.intersection_update(valid_keys | {k for k in self.rejected if not k[0].startswith('plan:')})
                self.city_success = self.clock()
                # Record decisions when their inputs/reason change, not every tick.
                reasons = {}
                for key in active:
                    prefix, reason = planned.decision(history[key], now)
                    n = planned.read_notice(history[key]['latest'])
                    reasons[key] = (n.fingerprint, n.publication, reason)
                    if self.city_reasons.get(key) != reasons[key]:
                        self.service.log.emit('traffic_notice_decision', event_id='plan:'+key,
                            outcome=('preview' if self.service.cfg.traffic_madison_preview else 'eligible') if prefix is not None else 'suppressed', reason=reason,
                            source=n.source_id, text=n.render(prefix or '', min(self.service.cfg.reply_max_chars, self.service._reply_max_bytes)))
                self.city_reasons = reasons
                self.service.log.emit('traffic_city_refresh', outcome='ready', reports=len(active),
                    omitted=getattr(self.city_api, 'omitted', 0), unavailable=getattr(self.city_api, 'unavailable', 0))
                return True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.service.log.emit('traffic_city_refresh', outcome='unavailable', reason=type(exc).__name__)
            return False

    def _planned_pending(self):
        if not self.city_fresh():
            return []
        return planned.candidates(self.state.get('plans', {}), self.city_active, self.wall_clock())

    def _city_conflicts_511(self, alert):
        for record in self.state.get('plans', {}).values():
            n = planned.read_notice(record['latest'])
            if n.status in planned.TERMINAL and planned.equivalent_511(replace(n, status='scheduled'), alert):
                return True
        return False

    def _reports(self):
        now = self.wall_clock()
        items = [a for a in self.current if self.fresh() and a.relevant(now, self.service.cfg.traffic_lookahead_h*3600)
                 and not self._city_conflicts_511(a)]
        if self.city_fresh():
            for key in sorted(self.city_active):
                r = self.state['plans'][key]
                n = planned.read_notice(r['latest'])
                if (n.status == 'scheduled' or r.get('announced')) and n.relevant(now):
                    prefix = 'Unconfirmed: ' if r.get('conflict') else ''
                    items.append(planned.PlannedAlert(key, n, prefix, 'requested report'))
        return items

    def fresh(self):
        return self.last_success is not None and 0 <= self.clock()-self.last_success < 600

    def eligible(self, alert):
        if isinstance(alert, planned.PlannedAlert):
            return (self.city_fresh() and alert.rejection_key not in self.rejected
                    and any(a.identifier == alert.identifier and a.fingerprint == alert.fingerprint
                            for a in self._planned_pending()))
        if self._city_conflicts_511(alert):
            return False
        if any(r.get('announced') and planned.equivalent_511(planned.read_notice(r['announced']), alert)
               for r in self.state.get('plans', {}).values()):
            return False
        return (self.fresh() and alert.relevant(self.wall_clock(), self.service.cfg.traffic_lookahead_h*3600)
                and alert.rejection_key not in self.rejected
                and self.state['seen'].get(alert.identifier, {}).get('fingerprint') != alert.fingerprint
                and not any(r.get('broadcast_key') == alert.broadcast_key and r.get('attempted', True)
                            for r in self.state['seen'].values())
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
        if isinstance(alert, planned.PlannedAlert):
            planned.claim(state['plans'][alert.key], self.wall_clock(), alert.reason)
        else:
            state['seen'][alert.identifier] = self._receipt(alert, self.wall_clock())
            for record in state.get('plans', {}).values():
                planned.inherit_511(record, alert, self.wall_clock())
        self._save(state)

    async def publish_pending(self):
        for alert in sorted([*self.current, *self._planned_pending()], key=lambda a: a.priority):
            if isinstance(alert, planned.PlannedAlert) and self.service.cfg.traffic_madison_preview:
                continue
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

    def answer(self, prompt, available, sender='', *, batch_end=True):
        raw = prompt.lower().strip()
        prefixed = raw.startswith(('/', '!'))
        q = ' '.join((raw[1:] if prefixed else raw).rstrip('.?!').split())
        if re.fullmatch(r'(?:belt\s*line|i[- ]?90)(?: (?:northbound|southbound|eastbound|westbound))?', q):
            q = 'traffic on '+q
        direction = cached_traffic_direction(q, self.service.cfg.web_location)
        if direction is not None:
            text = (self.service.traffic.answer(direction, available, include_source=False) if self.service.traffic is not None
                    else 'Travel-time lookup is disabled. Use /reports.')
        elif not prefixed:
            text = 'Use /reports for closures and incidents, or /help for instructions. Commands start with / or !.'
        elif q == 'help':
            text = '/reports sends up to 5 numbered closure and incident reports. /examples shows travel-time requests. /commands explains other commands.'
        elif q == 'examples':
            text = '/Beltline gives travel times both ways. Try /Beltline eastbound or /I90 northbound for one direction.'
        elif q == 'commands':
            text = '/details: source link. /status: data availability. /coverage: roads covered. /policy: automatic posts. /about: version.'
        elif q == 'policy':
            text = ('Automatic posts cover major incidents, closures and hazards, plus key changes or cancellations. Routine delays do not broadcast.'
                    if self.city_api is not None else
                    'Automatic posts cover major incidents, highway closures and dangerous road conditions. Routine delays do not broadcast.')
        elif q == 'coverage' and self.city_api is not None:
            text = f'Reports: {self.service.cfg.traffic_counties} County highways and Madison street closures. Travel times: Beltline and I90 (Beltline to I94).'
        elif q == 'coverage':
            text = f'Reports: highways in {self.service.cfg.traffic_counties}. Travel times: Madison Beltline and I90 (Beltline to I94).'
        elif q == 'source':
            text = 'https://github.com/mhcoen/meshpotato'
        elif q == 'readme':
            text = 'https://github.com/mhcoen/meshpotato/blob/main/bot/traffic/README.md'
        elif q in {'about', 'version'}:
            name, version = identity('traffic')
            sources = '511 + Madison' if self.city_api is not None else 'Wisconsin 511'
            text = f'{name} v{version}. Sources: {sources}. Computer + MeshCore radio.'
            link = ' https://github.com/mhcoen/meshpotato/blob/main/bot/traffic/README.md'
            if len((text+link).encode()) <= available:
                text += link
            elif len(text+' Use /source.') <= available:
                text += ' Use /source.'
        elif q == 'details':
            items = self._reports()
            signature = tuple(a.identifier+':'+a.fingerprint for a in items)
            previous_signature, index = self.cursors.get(sender, (None, 0))
            if previous_signature is None:
                text = 'Use /reports to read a report, then /details for its source.'
            elif signature != previous_signature:
                text = 'The report list changed. Use /reports to start again, then /details.'
            elif 1 <= index <= len(items) and isinstance(items[index-1], planned.PlannedAlert):
                link = items[index-1].notice.source_id
                text = link if len(link) <= available else 'Madison closure details: https://www.cityofmadison.com/transportation/news'
            else:
                text = '511 details: https://511wi.gov'
        elif q == 'status' and self.city_api is not None:
            wi = 'fresh' if self.fresh() else 'unavailable'
            city = ('partial' if (getattr(self.city_api, 'unavailable', 0) or getattr(self.city_api, 'omitted', 0)) else 'fresh') if self.city_fresh() else 'unavailable'
            text = f'511: {wi}. Madison notices: {city}. {len(self._reports())} reports. Use /reports.'
        elif q == 'status':
            if self.last_success is None:
                text = 'Waiting for verified 511 data. Announcements are quiet until the feed is available.'
            else:
                age = max(0, int((self.clock()-self.last_success)//60))
                text = f'511 checked {age} min ago; {len(self.current)} significant reports. '+('Use /reports.' if self.fresh() else 'Feed is stale; broadcasts paused.')
        elif q in {'reports', 'cont'}:
            if not self.fresh() and not self.city_fresh():
                text = 'I can get live traffic, but cannot verify the alerts right now. Please try again shortly.'
            else:
                items = self._reports()
                signature = tuple(a.identifier+':'+a.fingerprint for a in items)
                previous_signature, previous_index = self.cursors.get(sender, ((), 0))
                if q == 'cont':
                    index = previous_index+1 if previous_signature == signature else 1
                else:
                    index = 1
                if not items:
                    text = 'No significant reports in the available feeds. This is not a guarantee that roads are clear.'
                    if self.city_api is not None and (not (self.fresh() and self.city_fresh()) or (getattr(self.city_api, 'unavailable', 0) or getattr(self.city_api, 'omitted', 0))):
                        text = 'Coverage is incomplete. No reports in the available data; this does not mean roads are clear.'
                elif index > len(items):
                    text = 'End of reports. Use /reports to start again.'
                else:
                    partial = self.city_api is not None and (not (self.fresh() and self.city_fresh()) or (getattr(self.city_api, 'unavailable', 0) or getattr(self.city_api, 'omitted', 0)))
                    prefix = ('Partial coverage. ' if partial else '')+f'{index} of {len(items)}. '
                    suffix = (' Next 5: /cont' if index < len(items) else ' End of reports.') if batch_end else ''
                    detail = items[index-1].render(self.wall_clock(), available-len(prefix)-len(suffix))
                    if detail:
                        text = prefix+detail+suffix
                    else:
                        text = prefix+'Report too long. Use /details for the source.'+suffix
                    self.cursors.pop(sender, None)
                    self.cursors[sender] = (signature, index)
                    while len(self.cursors) > 500:
                        self.cursors.popitem(last=False)
        else:
            text = traffic_clarification(q, self.service.cfg.web_location) or 'Try /reports, /cont, /Beltline or /I90. Other road details: 511wi.gov. Use /help.'
        return text if len(text) <= available else 'Try /help, /status or /reports. Full traffic details: 511wi.gov.'
