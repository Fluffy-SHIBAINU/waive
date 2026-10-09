import io
import logging
import logging.config
import sys

import uvicorn.config
from pydantic import ValidationError

from waive.cases.extract import BillExtract
from waive.logging_setup import RedactingFilter, configure_logging


def test_filter_drops_records_with_personal_looking_content(caplog):
    configure_logging()
    logger = logging.getLogger("waive.test")
    with caplog.at_level(logging.INFO):
        logger.info("case %s evaluated tier=free", "abc123")
        logger.info("account ACCT-12345678 amount $1,850.00 patient rosa@example.org")
    messages = [record.getMessage() for record in caplog.records if record.name == "waive.test"]
    assert messages == ["case abc123 evaluated tier=free"]
    assert RedactingFilter().filter(logging.makeLogRecord({"msg": "amount $5"})) is False


UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")


def test_filter_reaches_uvicorn_handlers_and_waive_child_loggers(capsys):
    """`waive serve` and the container apply uvicorn's logging config before create_app(): the
    root logger has no handler and uvicorn's own handler carries no filter. Records created on
    child loggers (uvicorn.error, waive.*) never pass a filter attached to the parent Logger."""
    root = logging.getLogger()
    saved_root = (list(root.handlers), list(root.filters), root.level)
    saved = {
        name: (list(lg.handlers), list(lg.filters), lg.level, lg.propagate)
        for lg in (logging.getLogger(n) for n in (*UVICORN_LOGGERS, "waive.child"))
        for name in [lg.name]
    }
    saved_last_resort = list(logging.lastResort.filters)
    try:
        logging.config.dictConfig(uvicorn.config.LOGGING_CONFIG)
        root.handlers[:] = []
        configure_logging()
        (uvicorn_handler,) = logging.getLogger("uvicorn").handlers
        uvicorn_stream = io.StringIO()
        uvicorn_handler.setStream(uvicorn_stream)
        errors = logging.getLogger("uvicorn.error")
        errors.error("Exception in ASGI application: input_value='$1,850.00' rosa@example.org")
        errors.info("Application startup complete.")
        child = logging.getLogger("waive.child")
        child.warning("sealed amount $5 for rosa@example.org")
        child.info("atlas refresh: 3 hospitals checked")
        captured = capsys.readouterr().err
    finally:
        root.handlers[:], root.filters[:], level = saved_root
        root.setLevel(level)
        for name, (handlers, filters, level, propagate) in saved.items():
            logger = logging.getLogger(name)
            logger.handlers[:], logger.filters[:] = handlers, filters
            logger.setLevel(level)
            logger.propagate = propagate
        logging.lastResort.filters[:] = saved_last_resort
    uvicorn_text = uvicorn_stream.getvalue()
    assert "startup complete" in uvicorn_text
    assert "$" not in uvicorn_text and "@" not in uvicorn_text
    assert "$5" not in captured and "@" not in captured
    assert "3 hospitals checked" in captured  # waive.* INFO no longer vanishes into lastResort
    assert any(isinstance(f, RedactingFilter) for f in logging.lastResort.filters)


def _exc_info(raiser):
    try:
        raiser()
    except Exception:
        return sys.exc_info()
    raise AssertionError("expected an exception")


def _chained_validation_error():
    try:
        BillExtract.model_validate_json('{"amount_due": "$1,850.00", "patient_name": "Rosa"}')
    except ValidationError as error:
        raise RuntimeError("model output did not match BillExtract") from error


def test_filter_redacts_a_traceback_that_carries_personal_values():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(RedactingFilter())
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    logger = logging.getLogger("waive.test.tracebacks")
    logger.propagate = False
    logger.addHandler(handler)
    try:
        logger.error("Exception in ASGI application", exc_info=_exc_info(_chained_validation_error))
        logger.error("Exception in ASGI application", exc_info=_exc_info(lambda: 1 / 0))
    finally:
        logger.removeHandler(handler)
    text = stream.getvalue()
    assert text.count("ERROR Exception in ASGI application") == 2
    assert "1,850" not in text and "input_value" not in text and "Rosa" not in text
    assert "traceback redacted: RuntimeError" in text
    assert "ZeroDivisionError" in text and "Traceback" in text  # harmless tracebacks stay whole
