"""The cursor codec.

The property that matters is exact round-tripping. A cursor that loses a
microsecond does not fail loudly — it silently skips or repeats a document at
every page boundary, which is the failure mode keyset pagination exists to
avoid in the first place.
"""

import base64
from datetime import UTC, datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app.api.cursors import decode_cursor, encode_cursor
from app.domain.document import DocumentCursor


def make_cursor(**overrides) -> DocumentCursor:
    return DocumentCursor(
        **{
            "created_at": datetime(2026, 7, 26, 10, 23, 45, 123456, tzinfo=UTC),
            "id": uuid4(),
            **overrides,
        }
    )


def test_round_trip_is_exact_to_the_microsecond():
    cursor = make_cursor()

    assert decode_cursor(encode_cursor(cursor)) == cursor


def test_microsecond_precision_is_not_truncated():
    """One microsecond apart must not encode to the same cursor."""
    cursor = make_cursor()
    later = make_cursor(
        created_at=cursor.created_at + timedelta(microseconds=1), id=cursor.id
    )

    assert encode_cursor(cursor) != encode_cursor(later)
    assert decode_cursor(encode_cursor(later)).created_at == later.created_at


def test_a_non_utc_offset_survives_the_round_trip():
    paris = datetime(2026, 7, 26, 12, 23, 45, tzinfo=timezone(timedelta(hours=2)))

    decoded = decode_cursor(encode_cursor(make_cursor(created_at=paris)))

    assert decoded.created_at == paris


def test_the_token_is_opaque():
    """No part of the sort key is readable in the query string."""
    cursor = make_cursor()

    encoded = encode_cursor(cursor)

    assert str(cursor.id) not in encoded
    assert "2026" not in encoded
    # Unpadded, so nothing needs percent-encoding to survive a query string.
    assert "=" not in encoded


@pytest.mark.parametrize(
    "value",
    [
        "",
        "not base64 at all!!",
        # Valid base64, but not a cursor.
        base64.urlsafe_b64encode(b"nonsense").decode().rstrip("="),
        # Right shape, unparseable timestamp.
        base64.urlsafe_b64encode(b"yesterday|" + str(uuid4()).encode())
        .decode()
        .rstrip("="),
        # Right shape, id is not a UUID.
        base64.urlsafe_b64encode(b"2026-07-26T10:23:45+00:00|nope")
        .decode()
        .rstrip("="),
        # No separator.
        base64.urlsafe_b64encode(b"2026-07-26T10:23:45+00:00").decode().rstrip("="),
        # Non-UTF-8 payload.
        base64.urlsafe_b64encode(b"\xff\xfe").decode().rstrip("="),
    ],
)
def test_garbage_is_rejected(value):
    with pytest.raises(ValueError, match="malformed cursor"):
        decode_cursor(value)


def test_a_naive_timestamp_is_rejected():
    """It would compare against timestamptz and blow up in the driver."""
    naive = (
        base64.urlsafe_b64encode(f"2026-07-26T10:23:45.123456|{uuid4()}".encode())
        .decode()
        .rstrip("=")
    )

    with pytest.raises(ValueError, match="malformed cursor"):
        decode_cursor(naive)
