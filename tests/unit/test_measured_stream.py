import hashlib
from collections.abc import AsyncIterator

import pytest

from app.application.upload_document import MeasuredStream
from app.domain.errors import UploadTooLarge
from tests.fakes import chunks

pytestmark = pytest.mark.anyio


async def test_counts_and_hashes_in_one_pass():
    stream = MeasuredStream(chunks(b"hello ", b"world"), limit=1000)

    assert b"".join([c async for c in stream]) == b"hello world"
    assert stream.size == 11
    assert stream.sha256 == hashlib.sha256(b"hello world").hexdigest()


async def test_exactly_at_the_limit_is_allowed():
    stream = MeasuredStream(chunks(b"x" * 10), limit=10)

    assert b"".join([c async for c in stream]) == b"x" * 10


async def test_one_byte_over_the_limit_raises():
    stream = MeasuredStream(chunks(b"x" * 11), limit=10)

    with pytest.raises(UploadTooLarge):
        [c async for c in stream]


async def test_source_is_abandoned_as_soon_as_the_limit_is_crossed():
    consumed = 0

    async def counted() -> AsyncIterator[bytes]:
        nonlocal consumed
        for _ in range(100):
            consumed += 1
            yield b"x" * 10

    stream = MeasuredStream(counted(), limit=25)

    with pytest.raises(UploadTooLarge):
        [c async for c in stream]

    # Stopped at the third chunk (30 > 25) rather than draining all 100.
    assert consumed == 3
