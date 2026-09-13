"""Configuration: a TOML file with flat keys grouped into sections, plus MESHPOTATO_* env overrides.

Every key in the TOML file maps to one field of :class:`Config` regardless of which
section it sits in; sections exist only for readability. The environment variable
``MESHPOTATO_<FIELD_NAME_UPPER>`` overrides any field. The OpenAI-compatible API key is
deliberately *not* a config field: it is read from ``MESHPOTATO_OPENAI_API_KEY`` by the
backend so it can never end up in a log record or a dumped config.
"""

from __future__ import annotations

import os
import math
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from bot.fortune import parse_hhmm
from bot.personas import BUILTIN_PERSONAS, FORGET_COMMAND, HELP_COMMAND, MAGIC8_COMMAND, NAME_RE, RESET_COMMAND, ROLL_COMMAND, build_help
from bot.reply import plain_ascii

ENV_PREFIX = "MESHPOTATO_"
API_KEY_ENV = "MESHPOTATO_OPENAI_API_KEY"
LEGACY_ENV_PREFIX = "MESHAI_"
LEGACY_API_KEY_ENV = "MESHAI_OPENAI_API_KEY"

# MeshCore packs "<node name>: <text>" into at most this many bytes and silently truncates the rest.
WIRE_TEXT_MAX = 160

BACKENDS = ("ollama", "openai")
RX_LOG_MODES = ("off", "channel", "all")
THINK_MODES = ("off", "on", "omit")


class ConfigError(ValueError):
    """Raised for a malformed or invalid configuration."""


@dataclass(frozen=True)
class Config:
    # [radio]
    port: str = ""
    channel_idx: int = 1

    # [bot]
    bot_name: str = "Mesh Potato"
    trigger_prefix: str = ""
    reply_max_chars: int | None = None
    prompt_max_chars: int = 160
    reply_delay_s: float = 8.0
    shorten_retries: int = 2
    too_long_reply: str = "That answer will not fit in one message, ask me something narrower."
    apology: str = "Sorry, I couldn't answer that one."

    facts: str = ""  # local facts for the model, appended to the built-in LoRa facts and the radio's settings

    # [personas] table: name -> persona text (built-ins when absent), plus the keys below
    personas: dict[str, str] = field(default_factory=lambda: dict(BUILTIN_PERSONAS))
    default_persona: str = "funny"
    persona_timeout_min: float = 120.0
    persona_reset_message: str = "Back to the default personality."
    command_prefix: str = "/"

    # [model]
    backend: str = "ollama"
    model: str = "qwen3:30b-a3b-instruct-2507-q4_K_M"
    ollama_host: str = "http://127.0.0.1:11434"
    ollama_think: str = "off"
    ollama_keep_alive: str = "30m"
    openai_base_url: str = "http://127.0.0.1:1234/v1"
    temperature: float = 0.6
    max_tokens: int = 80
    model_timeout_s: float = 30.0

    # [fortune]
    fortune_enabled: bool = True
    fortune_time: str = "06:00"
    fortune_jitter_min: float = 12.0
    fortune_cutoff_min: float = 30.0
    fortune_prefix: str = "Fortune: "
    fortune_prompt: str = (
        "Write today's fortune for everyone on the channel: one silly, funny sentence in the style of a "
        "fortune cookie, somehow involving {subject}. Do not mention or address anyone. Today is {date}."
    )
    fortune_fallback: str = "The mesh is quiet this morning, and so is your fortune."

    # [limits]
    global_rate_per_min: float = 4.0
    global_burst: int = 1
    sender_rate_per_min: float = 4.0
    sender_burst: int = 1
    queue_max_pending: int = 10
    queue_wait_s: float = 600.0

    # [history]
    state_db: str = "meshpotato.sqlite3"  # empty disables persistence
    state_save_interval_s: float = 5.0
    history_size: int = 20
    history_max_age_s: float = 3600.0
    transcript_max_chars: int = 1500
    person_memory_rounds: int = 20  # answered exchanges remembered per sender name
    person_memory_days: float = 14.0  # rounds older than this are dropped
    person_memory_people: int = 500  # names remembered at once, least recently seen out first
    person_memory_max_chars: int = 600  # size of the remembered block given to the model

    # [adaptive]
    adaptive_enabled: bool = True
    utilization_poll_s: float = 10.0
    utilization_window_s: float = 120.0
    duty_low: float = 0.05
    duty_high: float = 0.15
    tx_duty_budget: float = 0.02

    # [injection]
    injection_threshold: float = 0.45

    # [logging]
    log_file: str = ""
    rx_log: str = "channel"  # "off" | "channel" (packets on the served channel) | "all" (every packet heard)

    def __post_init__(self) -> None:
        if self.reply_max_chars is None:
            object.__setattr__(self, "reply_max_chars", WIRE_TEXT_MAX - len(self.bot_name.encode("utf-8")) - 2)

    def validate(self) -> "Config":
        errors: list[str] = []
        for f in fields(self):
            value = getattr(self, f.name)
            if isinstance(value, float) and not math.isfinite(value):
                errors.append(f"{f.name} must be finite")
        if errors:
            raise ConfigError("; ".join(errors))
        for name in ("apology", "too_long_reply", "persona_reset_message", "fortune_fallback", "fortune_prefix", "command_prefix"):
            value = getattr(self, name)
            if any(not " " <= c <= "~" for c in value) or plain_ascii(value) != value.strip():
                errors.append(f"{name} must use printable ASCII and ordinary punctuation")
        for name in ("apology", "fortune_fallback"):
            if not getattr(self, name).strip():
                errors.append(f"{name} must not be empty")
        if not self.port:
            errors.append("port is required (radio.port or MESHPOTATO_PORT)")
        if not 0 <= self.channel_idx <= 255:
            errors.append("channel_idx must be in 0..255")
        if not self.bot_name.strip():
            errors.append("bot_name must not be empty")
        if ":" in self.bot_name:
            errors.append("bot_name must not contain a colon; the radio uses 'name: text' on the channel")
        if self.reply_max_chars > 0 and len(self.apology) > self.reply_max_chars - len("@[") - 20 - len("] "):
            errors.append("apology must fit after '@[sender] ' with room for a 20 character name")
        if self.backend not in BACKENDS:
            errors.append(f"backend must be one of {BACKENDS}")
        if self.rx_log not in RX_LOG_MODES:
            errors.append(f"rx_log must be one of {RX_LOG_MODES}")
        if self.ollama_think not in THINK_MODES:
            errors.append(f"ollama_think must be one of {THINK_MODES}")
        if not self.model:
            errors.append("model must not be empty")
        if self.reply_max_chars <= 0:
            errors.append("reply_max_chars must be positive")
        wire_room = WIRE_TEXT_MAX - len(self.bot_name.encode("utf-8")) - 2
        if self.reply_max_chars > wire_room:
            errors.append(
                f"reply_max_chars must be at most {wire_room} for bot_name {self.bot_name!r}: the radio packs "
                f"'{self.bot_name}: ' plus the reply into {WIRE_TEXT_MAX} bytes and cuts the rest"
            )
        if self.prompt_max_chars <= 0:
            errors.append("prompt_max_chars must be positive")
        if self.shorten_retries < 0:
            errors.append("shorten_retries must not be negative")
        if not self.too_long_reply.strip():
            errors.append("too_long_reply must not be empty")
        if len(self.too_long_reply) > self.reply_max_chars - 24:
            errors.append("too_long_reply must fit with room for a 20 character sender name")
        if self.reply_delay_s < 0:
            errors.append("reply_delay_s must not be negative")
        if self.model_timeout_s <= 0:
            errors.append("model_timeout_s must be positive")
        try:
            parse_hhmm(self.fortune_time)
        except ValueError as exc:
            errors.append(str(exc))
        if self.fortune_jitter_min < 0 or self.fortune_cutoff_min < 0:
            errors.append("fortune_jitter_min and fortune_cutoff_min must not be negative")
        if not self.fortune_prompt.strip() or "{subject}" not in self.fortune_prompt:
            errors.append("fortune_prompt must contain {subject}")
        else:
            try:
                self.fortune_prompt.format(subject="x", date="y")
            except (KeyError, IndexError, ValueError) as exc:
                errors.append(f"fortune_prompt has a bad placeholder ({exc}); only {{subject}} and {{date}} are allowed")
        if self.reply_max_chars > 0 and len(self.fortune_prefix + self.fortune_fallback + self.fortune_help_hint) > self.reply_max_chars:
            errors.append("fortune_prefix plus fortune_fallback and help hint must fit in reply_max_chars")
        if self.fortune_enabled and any(not " " <= c <= "~" for c in self.fortune_help_hint):
            errors.append("the fortune help hint requires printable ASCII in trigger_prefix and command_prefix")
        if not self.personas:
            errors.append("personas must not be empty")
        for name, text in self.personas.items():
            if not NAME_RE.fullmatch(name):
                errors.append(f"persona name {name!r} must be lowercase letters, digits, underscores, at most 16 chars")
            if name in (HELP_COMMAND, RESET_COMMAND, FORGET_COMMAND, ROLL_COMMAND, MAGIC8_COMMAND):
                errors.append(f"persona name {name!r} collides with a command")
            if not isinstance(text, str) or not text.strip():
                errors.append(f"persona {name!r} must have non-empty text")
        if self.default_persona not in self.personas:
            errors.append(f"default_persona {self.default_persona!r} is not in personas")
        if self.persona_timeout_min <= 0:
            errors.append("persona_timeout_min must be positive")
        if not self.command_prefix or " " in self.command_prefix:
            errors.append("command_prefix must be non-empty and contain no spaces")
        if not self.persona_reset_message.strip():
            errors.append("persona_reset_message must not be empty")
        room = self.reply_max_chars - len("@[") - 20 - len("] ")  # a 20 char sender name
        if self.reply_max_chars > 0:
            for number, page in enumerate(self.help_pages, 1):
                if len(page) > self.reply_max_chars:
                    suggestion = "shorten command_prefix" if number == 1 else "use fewer or shorter persona names or a shorter command_prefix"
                    errors.append(f"help page {number} is {len(page)} chars but reply_max_chars is {self.reply_max_chars}; {suggestion}")
        if self.reply_max_chars > 0 and len(self.persona_reset_message) > room:
            errors.append("persona_reset_message must fit with room for a 20 character sender name")
        if self.max_tokens <= 0:
            errors.append("max_tokens must be positive")
        for name in ("global_rate_per_min", "sender_rate_per_min"):
            if getattr(self, name) <= 0:
                errors.append(f"{name} must be positive")
        if self.queue_max_pending < 0:
            errors.append("queue_max_pending must not be negative")
        if self.queue_wait_s <= 0:
            errors.append("queue_wait_s must be positive")
        if self.person_memory_days <= 0 or self.person_memory_max_chars < 0:
            errors.append("person_memory_days must be positive and person_memory_max_chars not negative")
        if self.history_max_age_s <= 0 or self.state_save_interval_s <= 0:
            errors.append("history_max_age_s and state_save_interval_s must be positive")
        if self.state_db and (not self.state_db.strip() or "\x00" in self.state_db):
            errors.append("state_db must be a file path or an empty string")
        for name in ("global_burst", "sender_burst", "history_size", "person_memory_rounds", "person_memory_people"):
            if getattr(self, name) < 1:
                errors.append(f"{name} must be at least 1")
        if self.transcript_max_chars < 0:
            errors.append("transcript_max_chars must not be negative")
        if not 0.0 <= self.injection_threshold <= 1.0:
            errors.append("injection_threshold must be between 0 and 1")
        if self.utilization_poll_s <= 0 or self.utilization_window_s < self.utilization_poll_s:
            errors.append("utilization_poll_s must be positive and no larger than utilization_window_s")
        if not 0.0 < self.tx_duty_budget <= 0.5:
            errors.append("tx_duty_budget must be between 0 and 0.5")
        if not 0.0 <= self.duty_low < self.duty_high <= 1.0:
            errors.append("need 0 <= duty_low < duty_high <= 1")
        if errors:
            raise ConfigError("; ".join(errors))
        return self

    @property
    def fortune_help_hint(self) -> str:
        return f" Try {self.trigger_prefix}{self.command_prefix}{HELP_COMMAND}."

    @property
    def help_message(self) -> str:
        return build_help(list(self.personas), self.persona_timeout_min, self.command_prefix)

    @property
    def help_pages(self) -> tuple[str, str]:
        return (
            "1/2 Ask me anything, including LoRa questions or how your message reached me. "
            f"{self.command_prefix}{ROLL_COMMAND} rolls dice; "
            f"{self.command_prefix}{MAGIC8_COMMAND} answers yes/no questions.",
            self.help_message,
        )

    @property
    def default_persona_text(self) -> str:
        return self.personas[self.default_persona]


_FIELD_TYPES: dict[str, type] = {f.name: f.type for f in fields(Config)}  # type: ignore[misc]


def _coerce(name: str, value: Any) -> Any:
    """Coerce a TOML or env value to the declared field type."""
    if name == "personas":
        if not isinstance(value, Mapping):
            raise ConfigError("personas must be a table of name = \"text\"")
        return {str(k): str(v) for k, v in value.items()}
    target = _FIELD_TYPES[name]
    if isinstance(target, str):  # `from __future__ import annotations` leaves strings
        target = {"str": str, "int": int, "int | None": int, "float": float, "bool": bool}[target]
    if target is bool:
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in ("1", "true", "yes", "on"):
            return True
        if text in ("0", "false", "no", "off"):
            return False
        raise ConfigError(f"{name}: expected a boolean, got {value!r}")
    if target is int:
        if isinstance(value, bool):
            raise ConfigError(f"{name}: expected an integer, got {value!r}")
        try:
            return int(value)
        except (TypeError, ValueError, OverflowError):
            raise ConfigError(f"{name}: expected an integer, got {value!r}") from None
    if target is float:
        if isinstance(value, bool):
            raise ConfigError(f"{name}: expected a number, got {value!r}")
        try:
            return float(value)
        except (TypeError, ValueError):
            raise ConfigError(f"{name}: expected a number, got {value!r}") from None
    return str(value)


def _flatten(doc: Mapping[str, Any]) -> dict[str, Any]:
    """Merge top-level keys and one level of sections into a single flat dict."""
    flat: dict[str, Any] = {}
    for key, value in doc.items():
        if key == "personas":
            flat[key] = value  # a table of presets, not a section of settings
            continue
        items = value.items() if isinstance(value, Mapping) else [(key, value)]
        for name, val in items:
            if name not in _FIELD_TYPES:
                raise ConfigError(f"unknown config key: {name}")
            if name in flat:
                raise ConfigError(f"config key given twice: {name}")
            flat[name] = val
    return flat


def config_from_mapping(doc: Mapping[str, Any], env: Mapping[str, str] | None = None) -> Config:
    """Build a Config from an already-parsed TOML document plus env overrides."""
    values = {name: _coerce(name, val) for name, val in _flatten(doc).items()}
    env = os.environ if env is None else env
    for name in _FIELD_TYPES:
        if name == "personas":
            continue  # a table; no environment form
        raw = env.get(ENV_PREFIX + name.upper(), env.get(LEGACY_ENV_PREFIX + name.upper()))
        if raw is not None:
            values[name] = _coerce(name, raw)
    return Config(**values).validate()


def load_config(path: str | Path, env: Mapping[str, str] | None = None) -> Config:
    """Load ``path`` (TOML) and apply env overrides. Raises ConfigError on any problem."""
    p = Path(path)
    try:
        with p.open("rb") as fh:
            doc = tomllib.load(fh)
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {p}") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{p}: {exc}") from None
    return config_from_mapping(doc, env)
