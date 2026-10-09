"""A bundled abbreviated README and answers grounded in the running configuration."""
from functools import lru_cache
from importlib.resources import files
import math
import re


@lru_cache(maxsize=1)
def abbreviated_readme():
    text = files('bot').joinpath('README.short.txt').read_text(encoding='utf-8')
    if not text.isascii() or len(text) > 4096:
        raise ValueError('abbreviated README must be ASCII and at most 4096 characters')
    return 'Abbreviated README, bundled with this bot version: ' + ' '.join(text.split())


def runtime_reference(cfg):
    p = cfg.command_prefix
    return (abbreviated_readme() + ' Runtime facts: '
            f'Bot name {cfg.bot_name}; model {cfg.model}; backend {cfg.backend}. '
            f'One reply has at most {cfg.reply_max_chars} characters including its addressee, '
            f'and must also fit the radio byte limit. Generation and lookup share {cfg.model_timeout_s:g} seconds. '
            f'{p}help lists commands; {p}help web, {p}help voices, {p}help fun, {p}help privacy give details. '
            f'Usage tips are {"enabled" if cfg.tips_enabled else "disabled"}; configured slots '
            f'{cfg.tips_morning_time} and {cfg.tips_evening_time} host local time, with quiet-channel deferral. '
            f'Voices: {", ".join(p+n for n in cfg.personas)}. '
            f'Nondefault voices revert to {cfg.default_persona} after {cfg.persona_timeout_min:g} minutes; '
            f'{p}reset restores that voice for everyone. '
            f'{p}forget clears personal memory for the sender, not shared history or logs. '
            f'{p}roll rolls dice; {p}magic8 gives a playful random answer, not a prediction. '
            f'Memory retains up to {cfg.person_memory_rounds} answered exchanges per sender for '
            f'{cfg.person_memory_days:g} days; shared history expires after {cfg.history_max_age_s:g} seconds. '
            + (f'Web search is enabled, automatically or with {p}web. This bot provides live sports scores, '
               'standings, records and upcoming games for NFL, NBA, WNBA, MLB and NHL using structured ESPN feeds. '
               'These are the bot\'s own answers even when the language model was not called. '
               'Source snapshots may lag; only newly supplied evidence supports current facts. '
               if cfg.web_enabled else 'Web search and live sports lookup are currently disabled. '))


def factual_self_reply(prompt, cfg, active_persona, remaining_s=None):
    """Narrow factual intents only; creative requests still go to the model."""
    q = ' '.join(prompt.lower().replace('’', "'").split())
    p = cfg.command_prefix
    from bot.sports_queries import has_team
    if (re.search(r'\b(?:you|bot|mesh potato)\b', q)
            and re.search(r'\b(?:provide|provides|give|gives|support|supports)\b.*\b(?:live )?sports\b', q)
            and not has_team(q)):
        return ('Yes, I provide sports scores, standings and upcoming games. Which team?'
                if cfg.web_enabled else 'Sports lookup is currently disabled by my operator.')
    if re.search(r'\b(?:can|could|do|are) you\b.*\b(?:sports|scores|standings|schedules)\b', q) and not re.search(r'\b(?:today|tonight|yesterday|tomorrow)\b', q):
        # Named-team questions belong to sports lookup, not capability handling.
        if not has_team(q):
            return ('Yes, I provide sports scores, standings and upcoming games. Which team?'
                    if cfg.web_enabled else 'Sports lookup is currently disabled by my operator.')
    if re.search(r'\b(?:what|which) model (?:are you|do you|is running)|\byour model name\b', q):
        return f'My configured model is {cfg.model}.'
    if re.search(r'\b(?:upgrade|change|replace|switch)\b.*\b(?:your model|you|model you)|\b(?:you|your model)\b.*\b(?:upgraded|changed|replaced)\b', q):
        return 'My operator can change my configured model; I cannot upgrade myself.'
    if re.search(r'\b(?:one message|multiple messages|multipart|multi.part|longer answers)\b', q) and re.search(r'\b(?:you|your|reply|replies|answer)\b', q):
        return 'I send one short reply per question. Ask a follow-up for more detail.'
    if re.search(r'\b(?:stay serious|go back to joke|revert|expire|reset automatically)\b', q) and re.search(r'\b(?:you|it|voice|personality|mode|serious)\b', q):
        return f'Nondefault voices revert to {cfg.default_persona} after {cfg.persona_timeout_min:g} minutes.'
    if re.search(r'\b(?:what|which)\b.*\b(?:voice|personality|mode)\b.*\b(?:you|active|using)\b', q):
        expiry = f', reverting to {cfg.default_persona} in {max(0, math.ceil(remaining_s/60))} minutes' if remaining_s is not None else ', with no expiry'
        return f'My voice is {active_persona}{expiry}.'
    if re.fullmatch(r'(?:what does |how does )/?serious(?: mode)?(?: do| work)?[?.!]*', q):
        return 'Serious mode gives straight answers without jokes; it does not change my capabilities.'
    if re.search(r'\b(?:can|could|do) you\b.*\b(?:search|browse)\b.*\b(?:web|internet)\b', q):
        return f'Yes, I search when current information is needed, or use {p}web followed by a question.' if cfg.web_enabled else 'Web search is currently disabled by my operator.'
    if re.fullmatch(r'(?:what can you do|what is your purpose)[?.!]*', q):
        return f'I answer questions, chat, write short poems and play games. Use {p}help for commands.'
    return ''


def game_reply(prompt, prefix='/'):
    if re.fullmatch(r"(?:can|could|shall) we play (?:a |some )?games?[?.!]*", prompt.strip(), re.I):
        return f'Yes, try {prefix}roll for dice or {prefix}magic8 followed by a yes-or-no question.'
    return ''
