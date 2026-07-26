from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from app.domain.errors import ObjectNotFound
from app.infrastructure.storage.posix import PosixObjectStore
from tests.fakes import chunks

pytestmark = pytest.mark.anyio


async def collect(source: AsyncIterator[bytes]) -> bytes:
    return b"".join([chunk async for chunk in source])


@pytest.fixture
def store(tmp_path: Path) -> PosixObjectStore:
    return PosixObjectStore(tmp_path, chunk_size=8)


async def test_put_then_get_roundtrip(store: PosixObjectStore):
    written = await store.put("org/doc", chunks(b"hello ", b"world"))

    assert written == 11
    assert await collect(store.get("org/doc")) == b"hello world"


async def test_put_creates_nested_directories(store: PosixObjectStore, tmp_path: Path):
    await store.put("org-a/doc-1", chunks(b"x"))

    assert (tmp_path / "org-a" / "doc-1").read_bytes() == b"x"


async def test_failure_mid_stream_leaves_no_key_and_no_debris(
    store: PosixObjectStore, tmp_path: Path
):
    async def explodes() -> AsyncIterator[bytes]:
        yield b"first"
        raise RuntimeError("upstream died")

    with pytest.raises(RuntimeError, match="upstream died"):
        await store.put("org/doc", explodes())

    # The port's contract: complete or absent. Nothing partial, no temp file.
    with pytest.raises(ObjectNotFound):
        await collect(store.get("org/doc"))
    assert list(tmp_path.rglob("*.tmp")) == []
    assert [p for p in tmp_path.rglob("*") if p.is_file()] == []


async def test_cancellation_leaves_no_debris(store: PosixObjectStore, tmp_path: Path):
    async def cancelled() -> AsyncIterator[bytes]:
        yield b"first"
        raise BaseException("cancelled")  # noqa: TRY002

    with pytest.raises(BaseException, match="cancelled"):
        await store.put("org/doc", cancelled())

    assert [p for p in tmp_path.rglob("*") if p.is_file()] == []


async def test_get_missing_key_raises(store: PosixObjectStore):
    with pytest.raises(ObjectNotFound):
        await collect(store.get("org/nope"))


async def test_delete_is_idempotent(store: PosixObjectStore):
    await store.put("org/doc", chunks(b"x"))

    await store.delete("org/doc")
    await store.delete("org/doc")

    with pytest.raises(ObjectNotFound):
        await collect(store.get("org/doc"))


async def test_get_streams_in_chunks(store: PosixObjectStore):
    await store.put("org/doc", chunks(b"a" * 20))

    received = [chunk async for chunk in store.get("org/doc")]

    assert received == [b"a" * 8, b"a" * 8, b"a" * 4]


@pytest.mark.parametrize(
    "key",
    ["", "/absolute", "../escape", "org/../../escape", "org//doc", "org/./doc", "org/"],
)
async def test_keys_that_could_escape_the_root_are_refused(
    store: PosixObjectStore, key: str
):
    with pytest.raises(ValueError):
        await store.put(key, chunks(b"x"))


async def test_put_overwrites_atomically(store: PosixObjectStore):
    await store.put("org/doc", chunks(b"old"))
    await store.put("org/doc", chunks(b"new content"))

    assert await collect(store.get("org/doc")) == b"new content"
