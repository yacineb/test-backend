"""`ObjectStore` backed by a local directory.

The port's contract — a key exists complete or not at all — is met natively
here by write/fsync/rename, the same way an S3 adapter would meet it with
multipart upload and abort. Nothing is emulated.
"""

import os
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import anyio.to_thread

from app.domain.errors import ObjectNotFound


def _write_all(fd: int, data: bytes) -> None:
    while data:
        data = data[os.write(fd, data) :]


def _unlink_quietly(path: Path) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def _commit(tmp: Path, final: Path) -> None:
    """Atomically move `tmp` onto `final` and make the rename durable.

    fsync on the parent directory is what turns "atomic" into "atomic and
    survives a power cut": rename(2) is atomic on its own, but the directory
    entry can still be sitting in the page cache when the machine dies.
    """
    os.replace(tmp, final)
    dir_fd = os.open(final.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


class PosixObjectStore:
    """Every blocking call goes through a worker thread.

    Calling os.write directly from the event loop would stall every other
    request on that worker for the duration of the disk write — including the
    health check — and it would look perfectly fine until the service was under
    concurrent load.
    """

    def __init__(self, root: Path, chunk_size: int = 1024 * 1024) -> None:
        self._root = root
        self._chunk_size = chunk_size

    def _path(self, key: str) -> Path:
        # Keys are server-generated, so this should never fire. It exists
        # because this module owns the only place where a string becomes a
        # path, and that is the one place worth being certain about.
        if not key or key.startswith("/"):
            raise ValueError(f"invalid storage key: {key!r}")
        if any(segment in ("", ".", "..") for segment in key.split("/")):
            raise ValueError(f"invalid storage key: {key!r}")
        return self._root / key

    async def put(self, key: str, chunks: AsyncIterator[bytes]) -> int:
        final = self._path(key)
        tmp = final.parent / f".{final.name}.{uuid4().hex}.tmp"

        await anyio.to_thread.run_sync(
            lambda: final.parent.mkdir(parents=True, exist_ok=True)
        )

        written = 0
        try:
            fd = await anyio.to_thread.run_sync(
                lambda: os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            )
            try:
                async for chunk in chunks:
                    if chunk:
                        await anyio.to_thread.run_sync(_write_all, fd, chunk)
                        written += len(chunk)
                await anyio.to_thread.run_sync(os.fsync, fd)
            finally:
                await anyio.to_thread.run_sync(os.close, fd)
            await anyio.to_thread.run_sync(_commit, tmp, final)
        except BaseException:
            # Includes cancellation: an aborted upload leaves no temp file.
            await anyio.to_thread.run_sync(_unlink_quietly, tmp)
            raise

        return written

    async def get(self, key: str) -> AsyncIterator[bytes]:
        path = self._path(key)
        try:
            fd = await anyio.to_thread.run_sync(lambda: os.open(path, os.O_RDONLY))
        except FileNotFoundError:
            raise ObjectNotFound(key) from None
        try:
            while chunk := await anyio.to_thread.run_sync(
                os.read, fd, self._chunk_size
            ):
                yield chunk
        finally:
            await anyio.to_thread.run_sync(os.close, fd)

    async def delete(self, key: str) -> None:
        await anyio.to_thread.run_sync(_unlink_quietly, self._path(key))
