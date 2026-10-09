"""Logging that refuses to record anything that looks personal (spec §11)."""

import logging
import re
import sys
import traceback

SENSITIVE = re.compile(r"\d{7,}|\$\s?\d|@")


class RedactingFilter(logging.Filter):
    """Drops a record whose message looks personal. A record whose traceback does (a pydantic
    error quoting a bill value, say) keeps its message and loses the traceback, so an
    "Exception in ASGI application" line still reaches the operator."""

    def filter(self, record: logging.LogRecord) -> bool:
        if SENSITIVE.search(record.getMessage()) is not None:
            return False
        details = [record.exc_text or "", record.stack_info or ""]
        exc_info = record.exc_info if isinstance(record.exc_info, tuple) else None
        if exc_info and exc_info[0] is not None:
            details.append("".join(traceback.format_exception(*exc_info)))
        if any(SENSITIVE.search(detail) for detail in details):
            kind = exc_info[0].__name__ if exc_info and exc_info[0] else "exception"
            record.msg = f"{record.getMessage()} [traceback redacted: {kind}]"
            record.args = ()
            record.exc_info = None
            record.exc_text = None
            record.stack_info = None
        return True


def _attach(target: logging.Logger | logging.Handler) -> None:
    if not any(isinstance(f, RedactingFilter) for f in target.filters):
        target.addFilter(RedactingFilter())


def configure_logging(level: str = "INFO") -> None:
    root = logging.getLogger()
    root.setLevel(level)
    _attach(root)
    if not root.handlers:
        # Under uvicorn's logging config (`waive serve`, the container) the root has no handler,
        # so waive.* records would fall through to logging.lastResort: WARNING and up, unfiltered.
        root.addHandler(logging.StreamHandler(sys.stderr))
    # Logger filters only see records created on that exact logger, never on its children
    # (uvicorn.error, waive.db, ...), so every handler of every logger carries the filter: that is
    # where propagated records end up.
    loggers = [
        root,
        *(lg for lg in root.manager.loggerDict.values() if isinstance(lg, logging.Logger)),
    ]
    for logger in loggers:
        for handler in logger.handlers:
            _attach(handler)
    if logging.lastResort is not None:
        _attach(logging.lastResort)
    for name in ("waive", "uvicorn", "httpx", "openai"):
        _attach(logging.getLogger(name))
