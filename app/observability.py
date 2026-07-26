"""Structured logging and request correlation.

Application code logs through the standard library and never imports structlog:

    logging.getLogger(__name__).info("upload.accepted", extra={"document_id": ...})

structlog is configured here, once, purely as the renderer. That keeps the
dependency at the edge -- `domain` and `application` still import nothing but
the standard library -- and swapping the renderer never touches a call site.

Event names are dotted, stable identifiers (`upload.rejected.wrong_type`) rather
than sentences, so they can be grepped, counted and alerted on without parsing
prose. Everything variable goes in `extra`, never interpolated into the name.

## Levels

The point of a level is to answer "does anyone need to look at this?", so:

- **ERROR** -- the service failed at something it promised to do. A human should
  look. Storage rejected a write; the database refused a commit.
- **WARNING** -- a request was refused for a reason worth seeing in aggregate:
  bad credentials, an oversized or non-PDF upload, an unverified webhook.
  Unremarkable one at a time, a signal a thousand at a time. These are *client*
  errors, so they are deliberately not ERROR -- paging someone because a user
  uploaded a PNG is how alerting gets ignored.
- **INFO** -- durable state changed: a document stored, a pipeline step
  finished, a token issued. One line per business event. This is the level
  production runs at, and the volume is bounded by real work, not by traffic.
- **DEBUG** -- mechanics: what a file sniffed as, which branch ran. Off in
  production, and never inside a per-chunk loop.

## Never logged

Passwords, access or refresh tokens, HMAC signatures, and file contents. The
correlation fields below are enough to follow a request without any of them.
"""

import logging
import sys
from typing import Any

import structlog

# Third-party loggers that are useful at their own default level and far too
# chatty below it.
_NOISY = ("sqlalchemy.engine", "dbos", "alembic", "multipart", "asyncio")


def _shared_processors() -> list[Any]:
    return [
        # Pulls request_id / org_id / user_id in from the contextvars bound by
        # the request middleware, so every line is correlated without any call
        # site having to pass them.
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.ExtraAdder(),
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
    ]


def build_formatter(fmt: str = "json") -> logging.Formatter:
    """The renderer, on its own so tests can capture exactly what is written.

    contextvars are merged here rather than at the call site, so a test that
    only inspects LogRecords cannot see request_id or org_id -- which is why
    this is exposed rather than kept private to configure_logging.
    """
    renderer: Any = (
        structlog.dev.ConsoleRenderer()
        if fmt == "console"
        else structlog.processors.JSONRenderer()
    )
    return structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=_shared_processors(),
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            # format_exc_info only for the structured renderer; the console
            # renderer prints tracebacks itself and would double them up.
            *([] if fmt == "console" else [structlog.processors.format_exc_info]),
            renderer,
        ],
    )


def configure_logging(level: str = "INFO", fmt: str = "json") -> None:
    """Install the renderer on the root logger. Safe to call more than once."""
    shared = _shared_processors()

    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(build_formatter(fmt))

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())

    # WARNING, not the root level. `sqlalchemy.engine` emits every statement
    # *and its bound parameters* as soon as its effective level reaches INFO,
    # so inheriting an INFO root turns on full SQL echo -- thousands of lines
    # per request, containing filenames, digests and, on the auth path, the
    # email being looked up. Whether to echo SQL is DB_ECHO's decision, which
    # sets this logger explicitly when the engine is built; it is not something
    # the application log level should switch on by accident.
    #
    # At DEBUG the operator has asked for the firehose, so they get it.
    noisy_level = root.level if root.level <= logging.DEBUG else logging.WARNING
    for name in _NOISY:
        logging.getLogger(name).setLevel(noisy_level)


def bind_context(**values: Any) -> None:
    """Attach fields to every log line emitted for the rest of this request."""
    structlog.contextvars.bind_contextvars(**values)


def clear_context() -> None:
    structlog.contextvars.clear_contextvars()
