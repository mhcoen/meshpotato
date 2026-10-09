"""Reviewed, one-packet usage examples and a twice-daily low-priority scheduler."""
from __future__ import annotations

import asyncio
import random
from datetime import datetime, timedelta

from bot.fortune import next_fire, parse_hhmm
from bot.storage import StateError

# Stable identifiers let rotation survive edits, upgrades, and restarts. These
# are examples of questions, never canned answers to people's conversations.
TIPS = (
    ('intro', 'general', 'Ask about weather, sports, traffic, radio, or something fun. Try "{ask}What can you do?"'),
    ('weather-local', 'web', 'Try "{ask}weather" for local conditions and tomorrow\'s forecast.'),
    ('traffic-beltline', 'web', 'Try "{ask}What is the traffic on the Madison Beltline?"'),
    ('sports-packers-record', 'web', 'Try "{ask}What is the Packers record?"'),
    ('poem-cheese', 'general', 'Try "{ask}Write a tiny poem about cheese."'),
    ('radio-snr', 'general', 'Try "{ask}What does SNR mean?"'),
    ('weather-city', 'web', 'Traveling? Try "{ask}weather in Chicago, IL". Name a city for another location.'),
    ('sports-brewers', 'web', 'Try "{ask}What is the Brewers record?"'),
    ('traffic-east', 'web', 'Try "{ask}Madison Beltline eastbound delays?" for the eastbound travel-time report.'),
    ('joke-geese', 'general', 'Try "{ask}Tell me a short joke about geese."'),
    ('radio-rssi', 'general', 'Try "{ask}What does RSSI mean?"'),
    ('translate-goodnight', 'general', 'Try "{ask}How do you say good night in Spanish?"'),
    ('weather-verona', 'web', 'Try "{ask}weather in Verona, WI" for a nearby forecast.'),
    ('sports-next', 'web', 'Try "{ask}When is the Brewers next game?"'),
    ('traffic-west', 'web', 'Heading west? Try "{ask}Madison Beltline westbound traffic?"'),
    ('poem-moon', 'general', 'Try "{ask}Write a tiny poem about the moon."'),
    ('radio-spreading', 'general', 'Try "{ask}What does spreading factor change?"'),
    ('explain-simple', 'general', 'Try "{ask}Explain gravity like I am ten."'),
    ('weather-milwaukee', 'web', 'Try "{ask}weather in Milwaukee, WI" before a trip.'),
    ('sports-packers', 'web', 'Try "{ask}When is the Packers next game?"'),
    ('traffic-age', 'web', 'Beltline reports show their age. Try "{ask}Madison Beltline traffic?" Stale data is not called current.'),
    ('joke-potato', 'general', 'Try "{ask}Tell me a potato joke."'),
    ('radio-bandwidth', 'general', 'Try "{ask}How does LoRa bandwidth affect range?"'),
    ('translate-thanks', 'general', 'Try "{ask}How do you say thank you in French?"'),
    ('weather-wx', 'web', 'Short on typing? Try "{ask}wx" for a local weather summary.'),
    ('sports-niners', 'web', 'Try "{ask}What is the Niners record?" Nicknames can save typing.'),
    ('traffic-plain', 'web', 'Traffic questions can use ordinary words: "{ask}Any delays on the Madison Beltline?"'),
    ('poem-dog', 'general', 'Try "{ask}Write a tiny poem about a sleepy dog."'),
    ('radio-coding', 'general', 'Try "{ask}What is LoRa coding rate?"'),
    ('science-sky', 'general', 'Try "{ask}Why is the sky blue?"'),
    ('weather-middleton', 'web', 'Try "{ask}weather in Middleton, WI" for conditions and a forecast.'),
    ('sports-bucks', 'web', 'Try "{ask}When is the Bucks next game?"'),
    ('sports-both', 'web', 'Try "{ask}What is the Brewers record and when is their next game?"'),
    ('joke-duck', 'general', 'Try "{ask}Tell me a short joke about a rubber duck."'),
    ('radio-repeater', 'general', 'Try "{ask}What does a MeshCore repeater do?"'),
    ('science-rainbow', 'general', 'Try "{ask}How does a rainbow form?"'),
    ('weather-madison', 'web', 'Try "{ask}weather in Madison, WI". Weather summaries include today and tomorrow.'),
    ('sports-giants', 'web', 'Shared team names need context. Try "{ask}What is the SF Giants record?"'),
    ('sports-date', 'web', 'For an older score, include the team and an actual game date written as YYYY-MM-DD.'),
    ('poem-rain', 'general', 'Try "{ask}Write a tiny poem about rain on a roof."'),
    ('radio-antenna', 'general', 'Try "{ask}What does antenna gain mean?"'),
    ('science-moon', 'general', 'Try "{ask}Why does the moon have phases?"'),
    ('sports-followup', 'web', 'After asking about a team, try "{ask}When is their next game?" I remember recent team context.'),
    ('sports-league', 'web', 'For ambiguous team names, include the sport or league: "{ask}New York Giants NFL record?"'),
    ('poem-radio', 'general', 'Try "{ask}Write a tiny poem about messages crossing the night."'),
    ('radio-los', 'general', 'Try "{ask}Why does antenna height matter?"'),
    ('math-percent', 'general', 'Try "{ask}What is 15 percent of 80?"'),
    ('translate-welcome', 'general', 'Try "{ask}How do you say welcome in German?"'),
    ('rewrite-kind', 'general', 'Try "{ask}Make this friendlier: Please stop blocking the driveway."'),
    ('brainstorm-name', 'general', 'Try "{ask}Suggest three names for a robot potato."'),
    ('radio-duplicates', 'general', 'Try "{ask}How does MeshCore recognize duplicate messages?"'),
    ('science-thunder', 'general', 'Try "{ask}What causes thunder?"'),
    ('math-distance', 'general', 'Try "{ask}How many miles is 10 kilometers?"'),
    ('translate-text', 'general', 'Try "{ask}Translate bonjour into English."'),
    ('rewrite-short', 'general', 'Try "{ask}Shorten this: I am on my way and should arrive in about ten minutes."'),
    ('brainstorm-snack', 'general', 'Try "{ask}Suggest three picnic snacks."'),
    ('radio-signal', 'general', 'Try "{ask}What is the difference between RSSI and SNR?"'),
    ('science-seasons', 'general', 'Try "{ask}Why do we have seasons?"'),
    ('followup', 'general', 'After an answer, try "{ask}Explain that more simply." Short follow-up questions work.'),
    ('followup-example', 'general', 'After an explanation, try "{ask}Give me an example."'),
    ('capabilities', 'general', 'Try "{ask}What can you do?" for a short introduction.'),
    ('model', 'general', 'Curious about the bot? Try "{ask}What model are you?"'),
    ('help', 'general', 'For commands and help topics, send "{command}help".'),
    ('dice', 'general', 'For a dice roll, send "{command}roll 2 6". That rolls two six-sided dice.'),
    ('magic8', 'general', 'For a playful yes/no answer, send "{command}magic8 Should I have another cookie?"'),
    ('privacy', 'general', 'Try "{command}help privacy" to learn about memory. This is a shared radio channel.'),
    ('forget', 'general', 'Try "{command}forget" to clear my personal memory of you. Shared channel history and logs remain.'),
    ('voices', 'general', 'Try "{command}help voices" to see styles. Voice changes affect the whole channel.'),
)


def examples(cfg):
    """Only advertise enabled features, with the actual configured syntax."""
    result = []
    for identifier, category, template in TIPS:
        if cfg.redirects_traffic and identifier.startswith('traffic-'):
            continue
        if identifier == 'traffic-age' and not cfg.traffic_enabled:
            continue
        if category == 'web' and not cfg.web_enabled:
            continue
        if identifier == 'intro' and not cfg.web_enabled:
            template = 'Ask about radio, science, poems, jokes, or games. Try "{ask}What can you do?"'
        elif identifier == 'intro' and cfg.redirects_traffic:
            template = 'Ask about weather, sports, radio or poems here. For traffic, visit #traffic. Try "{command}help".'
        text = 'Potato tip: ' + template.format(ask=cfg.trigger_prefix,
                                              command=cfg.trigger_prefix + cfg.command_prefix)
        if len(text) <= cfg.reply_max_chars and len((cfg.bot_name + ': ' + text).encode()) <= 160:
            result.append((identifier, text))
    return result


class UsageTipScheduler:
    def __init__(self, service, log, *, now=datetime.now, rng=None):
        self.service, self.log = service, log
        self._now, self._rng = now, rng or random.Random()
        self._task = None
        self.next_at = None
        self.last_slot = None
        self.used = []
        self.tick_s = 30.0

    def restore(self):
        store = self.service._state_store
        if store is not None:
            state = store.load_usage_tips()
            self.last_slot = datetime.fromisoformat(state['last_slot']) if state['last_slot'] else None
            self.used = state['used']

    def _save(self):
        store = self.service._state_store
        if store is not None:
            store.save_usage_tips(self.last_slot.isoformat(), self.used)

    def start(self):
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name='usage-tips')

    async def stop(self):
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _sleep_until(self, target):
        while (remaining := (target - self._now()).total_seconds()) > 0:
            await asyncio.sleep(min(self.tick_s, remaining))

    async def _run(self):
        cfg = self.service.cfg
        while True:
            try:
                now = self._now()
                if self.last_slot is not None and now <= self.last_slot:
                    now = self.last_slot + timedelta(microseconds=1)
                self.next_at = min(next_fire(now, when, cfg.tips_jitter_min, self._rng)
                                   for when in (cfg.tips_morning_time, cfg.tips_evening_time))
                self.log.emit('tip_scheduled', at=self.next_at.isoformat())
                await self._sleep_until(self.next_at)
                await self.fire(self.next_at)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.log.emit('tip_error', error=f'{type(exc).__name__}: {exc}')
                await asyncio.sleep(self.tick_s)

    async def fire(self, slot):
        if self.last_slot is not None and slot <= self.last_slot:
            return False
        self.last_slot = slot
        try:
            self._save()  # Claim before any send; a restart cannot replay an ambiguous attempt.
            cfg = self.service.cfg
            deadline = min(slot + timedelta(minutes=cfg.tips_window_min),
                           (slot + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0))
            def eligible():
                now = self._now()
                return slot <= now < deadline and now.date() == slot.date() and not now.fold
            while eligible():
                if self.service.usage_tip_ready():
                    choices = examples(cfg)
                    practical = [tip for tip in choices if tip[0].startswith(('weather-', 'sports-', 'traffic-'))]
                    broad = [tip for tip in choices if tip not in practical]
                    evening_hour, evening_minute = parse_hhmm(cfg.tips_evening_time)
                    morning_slot = slot.hour * 60 + slot.minute < evening_hour * 60 + evening_minute
                    if morning_slot and practical:
                        # Rotate the three useful topics as well as their examples.
                        topics = [topic for topic in ('weather', 'sports', 'traffic')
                                  if any(key.startswith(topic + '-') for key, _ in practical)]
                        last = next((key.split('-')[0] for key in reversed(self.used)
                                     if key.split('-')[0] in topics), None)
                        topic = topics[(topics.index(last) + 1) % len(topics)] if last else topics[0]
                        choices = [tip for tip in practical if tip[0].startswith(topic + '-')]
                    else:
                        choices = broad or choices
                    unused = [tip for tip in choices if tip[0] not in self.used]
                    if not unused and choices:
                        pool_ids = {key for key, _ in choices}
                        used_in_pool = [key for key in self.used if key in pool_ids]
                        recent = used_in_pool[-min(10, len(choices)-1):] if len(choices) > 1 else []
                        unused = [tip for tip in choices if tip[0] not in recent]
                        self.used = [key for key in self.used if key not in pool_ids or key in recent]
                    if not unused:
                        self.log.emit('tip_skipped', reason='no-fitting-examples')
                        return False
                    identifier, text = self._rng.choice(unused)
                    self.used = (self.used + [identifier])[-128:]
                    self._save()  # Never repeat a possibly delivered example after a send error.
                    outcome = await self.service.post_usage_tip(text, eligible)
                    self.log.emit('tip_posted' if outcome == 'sent' else 'tip_skipped',
                                  tip_id=identifier, outcome=outcome, text=text)
                    return outcome == 'sent'  # No retry after ANY radio attempt.
                await self._sleep_until(min(deadline, self._now() + timedelta(seconds=self.tick_s)))
            self.log.emit('tip_skipped', reason='busy-or-expired')
        except StateError as exc:
            self.log.emit('tip_skipped', reason='state-error', error=str(exc))
        return False
