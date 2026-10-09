"""Build a fresh generative repair request, without a table of canned answers."""


def recovery_messages(original: list[dict[str, str]], available: int) -> list[dict[str, str]]:
    messages = [dict(message) for message in original]
    messages[0]['content'] += (
        ' The previous response attempts were unusable. Start afresh from the original '
        'message and its conversation context; do not reuse a rejected draft. '
        'For this repair, write a short, warm, cute response that is specifically apropos '
        'of what the person said. Answer a question if possible, acknowledge a correction, '
        'and supply the actual content of a creative request. A poem needs verse, not a promise. '
        'Use gentle playfulness only where appropriate; serious or sensitive subjects need care. '
        'Do not make fun of the person, recite a stock potato joke, echo their message, '
        'claim an action you did not perform, or invent facts. Retain all source-evidence rules. '
        'Do not output PASS or the generic "I could not produce a useful answer; please rephrase". '
        'If a reference is genuinely ambiguous, ask one specific, friendly clarification. '
        f'The complete reply body must fit {available} ASCII characters; aim for '
        f'{max(3, available // 8)} words. Return only the reply in the required output format.'
    )
    return messages
