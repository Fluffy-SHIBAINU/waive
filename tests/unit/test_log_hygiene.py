import logging

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
