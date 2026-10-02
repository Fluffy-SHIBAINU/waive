"""Logging that refuses to record anything that looks personal (spec §11)."""

import logging
import re

SENSITIVE = re.compile(r"\d{7,}|\$\s?\d|@")


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return SENSITIVE.search(record.getMessage()) is None


def _attach(target: logging.Logger | logging.Handler) -> None:
    if not any(isinstance(f, RedactingFilter) for f in target.filters):
        target.addFilter(RedactingFilter())


def configure_logging(level: str = "INFO") -> None:
    root = logging.getLogger()
    root.setLevel(level)
    _attach(root)
    # Logger filters only see records created on that logger, not on its children, so the
    # root handlers (where every propagated record ends up) carry the filter as well.
    for handler in root.handlers:
        _attach(handler)
    for name in ("waive", "uvicorn", "httpx", "openai"):
        _attach(logging.getLogger(name))
