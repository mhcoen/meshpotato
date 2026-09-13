"""Split a MeshCore channel message into sender and prompt.

The companion delivers channel text as ``"SenderName: message"``. The sender part is
whatever the transmitting node put there: it is unauthenticated and can be forged by
anyone on the channel. Nothing downstream may treat it as an identity.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ParsedMessage:
    sender: str
    body: str
    raw: str


def parse_channel_text(text: str) -> ParsedMessage:
    """Parse exactly as the upstream meshcore example does.

    ``sender`` is everything before the first colon, stripped. ``body`` is everything
    after it, with whitespace collapsed, or empty when there is no colon at all.
    The original text remains available for structural checks and diagnostics.
    """
    sender = text.split(":", 1)[0].strip()
    body = " ".join(text.split(":", 1)[1].split()) if ":" in text else ""
    return ParsedMessage(sender=sender, body=body, raw=text)


def extract_prompt(body: str, trigger_prefix: str) -> str | None:
    """Return the prompt if ``body`` is addressed to the bot, else None.

    With an empty trigger prefix every non-empty body is a prompt. Otherwise the body
    must start with the prefix exactly (case-sensitive); the prompt is what follows,
    stripped, and must be non-empty.
    """
    if not trigger_prefix:
        return body or None
    if not body.startswith(trigger_prefix):
        return None
    prompt = body[len(trigger_prefix):].strip()
    return prompt or None
