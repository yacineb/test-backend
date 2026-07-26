"""Logging configuration, and the one thing it must never switch on."""

import logging

import pytest

from app.observability import configure_logging


# Restore whatever the app installed at import time, so these tests cannot
# leave the root logger reconfigured for everything that runs after them.
@pytest.fixture(autouse=True)
def restore_root():
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    levels = {
        name: logging.getLogger(name).level for name in ("sqlalchemy.engine", "dbos")
    }
    yield
    root.handlers, root.level = handlers, level
    for name, value in levels.items():
        logging.getLogger(name).setLevel(value)


def test_an_info_root_does_not_turn_on_sql_echo():
    """SQLAlchemy logs every statement *and its bound parameters* once its
    effective level reaches INFO. Those parameters carry filenames, digests and
    the email being looked up on the auth path, so an application log level of
    INFO must not be what enables them."""
    configure_logging("INFO", "json")

    engine_logger = logging.getLogger("sqlalchemy.engine")

    assert not engine_logger.isEnabledFor(logging.INFO)
    assert engine_logger.isEnabledFor(logging.WARNING)


def test_our_own_loggers_are_unaffected_at_info():
    configure_logging("INFO", "json")

    logger = logging.getLogger("app.application.upload_document")

    assert logger.isEnabledFor(logging.INFO)
    # Not merely "level permits it": Alembic's fileConfig sets .disabled on
    # every logger its ini does not name, which silences app.* without
    # changing a single level. See migrations/env.py.
    assert not logger.disabled


def test_debug_is_an_explicit_request_for_the_firehose():
    """Someone who sets LOG_LEVEL=DEBUG is debugging and wants the SQL."""
    configure_logging("DEBUG", "json")

    assert logging.getLogger("sqlalchemy.engine").isEnabledFor(logging.DEBUG)


def test_configuring_twice_does_not_stack_handlers():
    configure_logging("INFO", "json")
    configure_logging("INFO", "console")

    assert len(logging.getLogger().handlers) == 1
