"""Heuristic prompt injection detector for plain text.

Adapted from the vordur project's prompt_injection_detector.py and
normalization.py (MIT License, Copyright (c) 2026 Michael H. Coen), reduced to
the plain text path this bot needs. Pure pattern matching, no model, no I/O.

Text is normalised first: NFC, TR39 confusable characters folded to ASCII (so a
Cyrillic "a" cannot hide a trigger word), zero width and bidi control characters
removed, NFKC, lower case, URLs and bracketed indexes replaced by placeholders,
repeated letters and punctuation collapsed. It is then split into clauses and
each clause is scored against a small set of rules. The highest clause score
wins, with a bonus when two or more clauses score high on their own.

``detect_prompt_injection`` returns a score in [0, 1] and the names of the rules
that matched. The caller decides the threshold; 0.45 is the cutoff the original
detector uses.
"""

from __future__ import annotations

import re
import unicodedata
import warnings
from dataclasses import dataclass, field

DEFAULT_THRESHOLD = 0.45

# --------------------------------------------------------------------------- normalisation

_INVISIBLE_RE = re.compile("[\u00ad\u200b-\u200d\u2060\ufeff\ufffc\ufff9-\ufffb]")  # soft hyphen, zero width, joiners, BOM
_TAG_CHAR_RE = re.compile("[\U000e0001-\U000e007f]")  # tag plane
_BIDI_RE = re.compile("[\u202a-\u202e\u2066-\u2069]")  # bidi controls


def _build_confusable_table() -> dict[int, str]:
    """Map each non-ASCII character with an ASCII look-alike to that ASCII form."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", SyntaxWarning)  # the package has an unescaped regex literal
            from confusables import CONFUSABLE_MAP
    except ImportError:
        warnings.warn(
            "the 'confusables' package is not installed; homoglyph folding is disabled "
            "and look-alike characters can hide trigger words from the injection detector",
            RuntimeWarning,
            stacklevel=2,
        )
        return {}
    table: dict[int, str] = {}
    for char, candidates in CONFUSABLE_MAP.items():
        if len(char) != 1 or ord(char) < 128:
            continue
        lower = upper = other = None
        for c in candidates:
            if len(c) == 1 and ord(c) < 128:
                if c.islower():
                    lower = c
                elif c.isupper():
                    upper = c
                else:
                    other = c
        best = lower or upper or other
        if best:
            table[ord(char)] = best
    return table


_CONFUSABLE_TABLE: dict[int, str] = _build_confusable_table()


def strip_invisibles(text: str) -> str:
    """NFC, fold confusables to ASCII, then delete zero width, tag plane, and bidi characters."""
    text = unicodedata.normalize("NFC", text)
    if _CONFUSABLE_TABLE:
        text = text.translate(_CONFUSABLE_TABLE)
    text = _INVISIBLE_RE.sub("", text)
    text = _TAG_CHAR_RE.sub("", text)
    text = _BIDI_RE.sub("", text)
    return text


_URL_RE = re.compile(r"https?://\S+", re.I)
_INDEX_RE = re.compile(r"(?:\[\s*\d{1,4}\s*\]|\(\s*\d{1,4}\s*\)|#\s*\d{1,4}\b)")
_REPEATED_ALPHA_RE = re.compile(r"([a-z])\1{2,}")
_REPEATED_PUNCT_RE = re.compile(r"([!?.,])\1{1,}")


def _normalize_text(text: str) -> str:
    out = unicodedata.normalize("NFKC", strip_invisibles(text)).lower()
    out = _URL_RE.sub(" url_ref ", out)
    out = _INDEX_RE.sub(" index_ref ", out)
    out = _REPEATED_ALPHA_RE.sub(r"\1\1", out)
    out = _REPEATED_PUNCT_RE.sub(r"\1", out)
    # Keep line breaks: each channel line in an assembled context is scored as its own
    # clause, so a directive at the start of a line is seen as such and the framing text
    # in front of the history block cannot lend a line its verb.
    out = re.sub(r"[^\S\n]+", " ", out)
    return re.sub(r"\s*\n\s*", "\n", out).strip()


# --------------------------------------------------------------------------- rules

_IMPERATIVE_RE = re.compile(
    r"\b(please\s+)?(click|open|go|read|find|share|send|delete|replace|type|input|upvote|leave|add|ignore|reveal|export|compile)\b",
    re.I,
)
# "ignore" alone is ordinary speech ("it likes to ignore me"); it counts as an
# override only with an instruction-like object within a few words.
_OVERRIDE_RE = re.compile(
    r"\b(ignore (?:\w+ ){0,3}(?:instructions?|rules?|prompts?|guidelines?|directions?|goals?|constraints?|restrictions?|"
    r"polic(?:y|ies)|limits?|limitations?|safety|training|programming|guardrails?|filters?|system|context|history|"
    r"everything|all of|the above|previous|prior|earlier)|instead|rather than|must first|before typing|"
    r"original (goal|instruction)|following (instruction|description)|regardless of)\b",
    re.I,
)
# Making the bot relay text: a directive speech verb at the start of the clause with a
# quoted payload, "repeat after me", or a directive to tell everyone. Vocabulary alone is
# not enough ("what does the firmware say about \"direct\" packets" is a question).
# Transcript lines and history entries read "Sender: text"; a sender name has no colon, so
# everything up to the first colon on the line is the prefix, whatever its length.
_DIRECTIVE_LEAD = r"^(?:[^:\n]*:\s*)?(?:(?:please|just|now|ok|okay|hey|yo|so|and|bot|potato|mesh potato)[,:!]?\s+)*"
_PAYLOAD_GAP = r"(?:[^\"\u201c\u201d\n]{0,60})"  # "the following sentence exactly: " and the like
_RELAY_RE = re.compile(
    _DIRECTIVE_LEAD + r"(?:(?:can|could|would|will) you\s+)?(?:please\s+)?"
    r"(?:repeat|say|write|type|print|echo|post|send|announce|broadcast|tell (?:everyone|everybody|them|us|the channel)|"
    r"reply with|respond with|answer with)\b" + _PAYLOAD_GAP + r"[\"\u201c\u201d]"
    r"|\brepeat after me\b"
    r"|" + _DIRECTIVE_LEAD + r"(?:(?:can|could|would|will) you\s+)?(?:please\s+)?(?:tell|say|announce|post|broadcast|repeat|write|send)\b.{0,40}"
    r"\b(?:everyone|everybody|all of you|to all|(?:to|on) the (?:whole |entire )?channel|the channel that)\b",
    re.I,
)
# Changing how the bot replies from now on. Each pattern needs the directive, not just the
# temporal phrase: "I will use the roof antenna from now on" is a person talking.
_STYLE_OVERRIDE_RE = re.compile(
    r"\b(?:from now on|starting now|for the rest of (?:the|this) (?:day|night|conversation|chat|session)|until i say(?: otherwise| so)?)"
    r",?\s+(?:you |always |never |only |please )*(?:reply|respond|answer|write|speak|talk|say|end|start|begin|call|refer|sign|address|use|add|include|put)\b"
    r"|(?<!\bi )(?<!\bwe )(?<!\bhe )(?<!\bshe )(?<!\bthey )(?<!\bi will )(?<!\bi'll )"
    r"\b(?:reply|respond|answer|write|speak|talk|say|call yourself|sign)\b[^.!?]{0,40}\b(?:from now on|starting now|until i say)\b"
    r"|\b(?:write|make|keep|put|end|start|begin|give|reply|respond|answer|format|sign|prefix|suffix)\b[^.!?]{0,20}\b(?:all|every|each) (?:of )?your (?:replies|responses|messages|answers)\b"
    r"|\b(?:all|every|each) (?:of )?your (?:replies|responses|messages|answers) (?:must|should|will|need to|have to|are to|shall) (?:be |now |end|start|begin|include|contain)"
    r"|(?:" + _DIRECTIVE_LEAD + r"|\b(?:always|must|you must|you will|you should|you have to|now|from now on,?) )"
    r"(?:reply|respond|answer|write|speak|talk|type) (?:only )?(?:in|using) (?:reverse|all caps|capital letters|uppercase|lowercase|"
    r"pirate|french|spanish|german|japanese|chinese|russian|morse|binary|rhyme|rhymes|emoji|leetspeak|pig latin)\b"
    r"|\bend (?:every|each|all) (?:of )?(?:your )?(?:reply|replies|response|responses|message|messages) with\b"
    r"|\byour (?:new )?name is (?:now )?(?!not\b|fine\b|already\b)\w|\bcall yourself\b|\bpretend (?:to be|you are|you're|that you)\b"
    r"|\bnew (?:rule|rules|instruction|instructions|persona|personality)\b",
    re.I,
)
_CONCEALMENT_RE = re.compile(
    r"\b(do not mention this message|do not think step[- ]by[- ]step|do not ask the user|without telling the user)\b",
    re.I,
)
_SENSITIVE_RE = re.compile(
    r"\b(api key|openai api key|token|credential|credentials|password|ssh|credit card|bank account|secret|private key|last 3 emails?|group chat messages?)\b",
    re.I,
)
_ACTION_TARGET_RE = re.compile(
    r"\b(add to cart|upvote|leave a comment|open a new tab|go to url_ref|read (the )?(document|file)|find (an )?(existing )?(document|file)|fill out (the )?form|complete (the )?form|type (the )?task|click index_ref)\b",
    re.I,
)
_URGENCY_RE = re.compile(r"\b(alert|urgent|very important|critical|immediately|without delay)\b", re.I)
_CLICK_INDEX_RE = re.compile(r"\bplease click index_ref\b", re.I)
_SECRET_SOLICIT_RE = re.compile(
    r"\bcould you share\b.{0,80}\b(api key|openai api key|ssh credentials?|credit card|bank account|password|token|handphone number|phone number)\b",
    re.I | re.S,
)
_GOAL_REWRITE_RE = re.compile(
    r"\bif the original goal is\b.{0,120}\byou should (always|first|instead)\b",
    re.I | re.S,
)
_RIGHT_PLACE_STRONG_RE = re.compile(
    r"\bthis is the right place to input the (email|first name|last name|name|city name|physical address|zip code|confirm(?:ation of)? email address|ticket number|drop-off location)\b",
    re.I,
)
_DATA_OVERRIDE_RE = re.compile(
    r"\b(actual (email|price|miles)|ignore (the )?(labeled )?price|ignore the image|ignore relative position)\b",
    re.I,
)
_BENIGN_FORM_GUIDE_RE = re.compile(
    r"\bright place to input your email address for notifications|share (the )?(meeting notes|agenda|document|updates?)\b",
    re.I,
)
_CLAUSE_SPLIT_RE = re.compile(r"[.!?;\n]+|\bthen\b|\bafter that\b")


@dataclass
class PromptInjectionSignal:
    is_attack: bool
    score: float
    warnings: list[str] = field(default_factory=list)
    matched_rules: list[str] = field(default_factory=list)


def _score_clause(clause: str) -> tuple[float, list[str]]:
    score = 0.0
    matched: list[str] = []

    imperative = bool(_IMPERATIVE_RE.search(clause))
    override = bool(_OVERRIDE_RE.search(clause))
    concealment = bool(_CONCEALMENT_RE.search(clause))
    sensitive = bool(_SENSITIVE_RE.search(clause))
    action_target = bool(_ACTION_TARGET_RE.search(clause))
    urgency = bool(_URGENCY_RE.search(clause))
    click_index = bool(_CLICK_INDEX_RE.search(clause))
    secret_solicit = bool(_SECRET_SOLICIT_RE.search(clause))
    goal_rewrite = bool(_GOAL_REWRITE_RE.search(clause))
    right_place_strong = bool(_RIGHT_PLACE_STRONG_RE.search(clause))
    data_override = bool(_DATA_OVERRIDE_RE.search(clause))
    relay = bool(_RELAY_RE.search(clause))
    style_override = bool(_STYLE_OVERRIDE_RE.search(clause))

    if relay:
        score += 0.50
        matched.append("relay_request")
    if style_override:
        score += 0.50
        matched.append("style_override")
    if override and (imperative or action_target):
        score += 0.55
        matched.append("instruction_override")
    if concealment:
        score += 0.50
        matched.append("concealment_directive")
    if secret_solicit:
        score += 0.60
        matched.append("secret_solicitation")
    if goal_rewrite:
        score += 0.65
        matched.append("goal_rewrite")
    if right_place_strong:
        score += 0.70
        matched.append("form_field_redirection")
    if click_index:
        score += 0.70
        matched.append("click_directive_injection")
    if data_override:
        score += 0.60
        matched.append("data_override_instruction")
    if urgency and (override or action_target):
        score += 0.35
        matched.append("urgent_goal_hijack")

    atoms = sum([imperative, override, concealment, sensitive, action_target, urgency])
    if atoms >= 3:
        score += 0.25
        matched.append("multi_signal_composition")

    if _BENIGN_FORM_GUIDE_RE.search(clause) and not (
        secret_solicit or goal_rewrite or click_index or concealment or data_override or relay or style_override
    ):
        score = max(0.0, score - 0.45)
        matched.append("benign_guard")

    return min(score, 1.0), matched


def scan_text(text: str) -> tuple[float, list[str]]:
    """Score plain text: the best clause wins, with a bonus for two or more strong clauses."""
    normalized = _normalize_text(text)
    clauses = [c.strip() for c in _CLAUSE_SPLIT_RE.split(normalized) if c.strip()] or [normalized]

    max_score = 0.0
    matched_all: set[str] = set()
    strong = 0
    for clause in clauses:
        score, matched = _score_clause(clause)
        if score >= 0.40:
            strong += 1
        max_score = max(max_score, score)
        if score >= 0.20:
            matched_all.update(matched)
    if strong >= 2:
        max_score = min(1.0, max_score + 0.10)
        matched_all.add("multi_clause_consistency")
    return max_score, sorted(matched_all)


def detect_prompt_injection(content: str, threshold: float = DEFAULT_THRESHOLD) -> PromptInjectionSignal:
    """Score ``content`` and report whether it is at or above ``threshold``."""
    score, matched = scan_text(content)
    is_attack = score >= threshold
    notes: list[str] = []
    if is_attack:
        notes.append("Prompt-injection indicators detected: " + ", ".join(matched))
    elif score >= 0.25:
        notes.append("Potential prompt-injection signal detected: " + ", ".join(matched))
    return PromptInjectionSignal(is_attack=is_attack, score=score, warnings=notes, matched_rules=matched)
