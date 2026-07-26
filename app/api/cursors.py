"""Wire format for the document listing cursor.

The cursor *is* the sort key — `(created_at, id)` — base64url-encoded so it
reads as an opaque token. That is the whole point of the encoding: a client that
can read `2026-07-26T10:23:45.123456+00:00|3f2a…` will start constructing them,
and then the sort key can never change. Encoded, it is ours to redefine.

It is deliberately **not signed**. A signature would protect authority the
cursor does not carry: the organization comes from the bearer token and is
enforced again by row-level security, so the worst a forged cursor can do is
start the caller's own listing at a position of their choosing — which is what
the parameter is for. HMAC here would be security theatre with a key to rotate.
"""

import base64
import binascii
from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import Depends, HTTPException, Query, status

from app.domain.document import DocumentCursor

_SEPARATOR = "|"


def encode_cursor(cursor: DocumentCursor) -> str:
    raw = f"{cursor.created_at.isoformat()}{_SEPARATOR}{cursor.id}"
    # Unpadded: "=" is legal in a query string but gets percent-encoded by some
    # clients and not others, which makes cursors look different when they are
    # not. decode_cursor puts the padding back.
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(value: str) -> DocumentCursor:
    """Raise ValueError on anything this module did not produce."""
    try:
        padded = value + "=" * (-len(value) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        timestamp, separator, document_id = raw.partition(_SEPARATOR)
        if not separator:
            raise ValueError("cursor has no id")
        created_at = datetime.fromisoformat(timestamp)
        if created_at.tzinfo is None:
            # A naive timestamp compared against timestamptz is a driver-level
            # error at query time, i.e. a 500. Refuse it here as a 400 instead.
            raise ValueError("cursor timestamp has no timezone")
        return DocumentCursor(created_at=created_at, id=UUID(document_id))
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise ValueError("malformed cursor") from exc


def parse_cursor(
    cursor: Annotated[
        str | None,
        Query(
            description=(
                "Opaque token from a previous response's `next_cursor`. "
                "Omit it to start at the newest document."
            )
        ),
    ] = None,
) -> DocumentCursor | None:
    if cursor is None:
        return None
    try:
        return decode_cursor(cursor)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="invalid cursor"
        ) from exc


CursorDep = Annotated[DocumentCursor | None, Depends(parse_cursor)]
