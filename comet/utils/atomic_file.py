import asyncio
import os
import secrets
from pathlib import Path

import aiofiles


def _open_private(path: str, flags: int) -> int:
    flags |= os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    return os.open(path, flags, 0o600)


def _fsync_directory(directory: Path) -> None:
    directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


async def write_text_atomic(path: Path, content: str) -> None:
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    try:
        async with aiofiles.open(
            temporary,
            "w",
            encoding="utf-8",
            opener=_open_private,
        ) as file:
            await file.write(content)
            await file.flush()
            await asyncio.to_thread(os.fsync, file.fileno())
        await asyncio.to_thread(os.replace, temporary, path)
        await asyncio.to_thread(_fsync_directory, path.parent)
    finally:
        await asyncio.to_thread(temporary.unlink, missing_ok=True)
