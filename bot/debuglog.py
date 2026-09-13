"""Keep SDK secrets and raw transport dumps out of diagnostic logs."""

import logging
import re


class SafeDebugFilter(logging.Filter):
    # Raw frames can carry keys even before CHANNEL_INFO is parsed. Filter on
    # the handler, before any connection starts, not after get_channel returns.
    _sensitive = re.compile(
        r"channel[_ ](?:secret|info)|private[_ ]key|api[_ -]?key|authorization|"
        r"received data:|sending pkt\s*:|dispatching event:|"
        r"\b[0-9a-f]{32,}\b|(?:\\x[0-9a-f]{2}){4,}", re.I
    )

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if self._sensitive.search(message):
            record.msg = "[sensitive SDK diagnostic omitted]"
            record.args = ()
            record.exc_info = None
            record.exc_text = None
            record.stack_info = None
        elif record.exc_info or record.exc_text or record.stack_info:
            # Exception arguments and source lines may carry arbitrary secrets
            # even when the logged message itself is harmless.
            record.msg = message + " [traceback/stack omitted]"
            record.args = ()
            record.exc_info = None
            record.exc_text = None
            record.stack_info = None
        return True


def debug_handler(path: str | None, stream=None) -> logging.Handler:
    handler = logging.FileHandler(path, encoding="utf-8") if path else logging.StreamHandler(stream)
    handler.addFilter(SafeDebugFilter())
    handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
    return handler
