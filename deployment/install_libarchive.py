#!/usr/bin/env python3
"""Fetch and safely unpack the checksum-pinned libarchive source release."""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import io
import os
import tarfile
import urllib.request
from pathlib import Path, PurePosixPath

VERSION = "3.8.8"
SOURCE_FILENAME = f"libarchive-{VERSION}.tar.xz"
SOURCE_URL = f"https://www.libarchive.org/downloads/{SOURCE_FILENAME}"
SOURCE_SHA256 = "3873a88801da067d0528a989af06877710529d50ee8fe6f3970cbb4302efb918"
SOURCE_ROOT = f"libarchive-{VERSION}"
EXPECTED_VERSION = f"libarchive {VERSION}".encode()
MAX_DOWNLOAD_BYTES = 16 * 1024 * 1024
MAX_EXPANDED_BYTES = 64 * 1024 * 1024
MAX_MEMBER_BYTES = 16 * 1024 * 1024
MAX_MEMBERS = 5_000
MAX_SOURCE_TIMESTAMP = 4_102_444_800  # 2100-01-01 UTC


def _download(url: str, maximum_bytes: int) -> bytes:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "Comet-container-build"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        if response.geturl().split(":", 1)[0] != "https":
            raise RuntimeError("download redirected away from HTTPS")
        payload = response.read(maximum_bytes + 1)
    if len(payload) > maximum_bytes:
        raise RuntimeError("download exceeds its size limit")
    return payload


def _verify(payload: bytes) -> None:
    if hashlib.sha256(payload).hexdigest() != SOURCE_SHA256:
        raise RuntimeError("libarchive source SHA-256 mismatch")


def _safe_relative_path(member: tarfile.TarInfo) -> Path:
    name = member.name
    pure = PurePosixPath(name)
    if (
        not name
        or name.startswith("/")
        or "\\" in name
        or pure.parts[0] != SOURCE_ROOT
        or any(part in {"", ".", ".."} for part in pure.parts)
    ):
        raise RuntimeError("source archive contains an unsafe path")
    relative = pure.relative_to(SOURCE_ROOT)
    if not relative.parts:
        if not member.isdir():
            raise RuntimeError("source archive root is not a directory")
        return Path()
    if not (member.isdir() or member.isreg()):
        raise RuntimeError("source archive contains a special entry")
    return Path(*relative.parts)


def extract_source(payload: bytes, output: Path) -> None:
    """Extract one bounded, regular-file-only source tree."""
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:xz") as archive:
        members = archive.getmembers()
        if not members or len(members) > MAX_MEMBERS:
            raise RuntimeError("source archive member limit exceeded")
        validated: list[tuple[tarfile.TarInfo, Path]] = []
        expanded = 0
        seen: set[Path] = set()
        for member in members:
            relative = _safe_relative_path(member)
            if relative in seen:
                raise RuntimeError("source archive contains duplicate paths")
            seen.add(relative)
            if member.size < 0 or member.size > MAX_MEMBER_BYTES:
                raise RuntimeError("source archive member size limit exceeded")
            if member.mtime < 0 or member.mtime > MAX_SOURCE_TIMESTAMP:
                raise RuntimeError("source archive timestamp is invalid")
            expanded += member.size
            if expanded > MAX_EXPANDED_BYTES:
                raise RuntimeError("source archive expanded size limit exceeded")
            validated.append((member, relative))
        if Path("configure") not in seen or Path("COPYING") not in seen:
            raise RuntimeError("source archive lacks required build or license files")

        output.mkdir(mode=0o755, parents=False, exist_ok=False)
        try:
            for member, relative in validated:
                if not relative.parts:
                    continue
                target = output / relative
                if member.isdir():
                    target.mkdir(mode=0o755, parents=True, exist_ok=False)
                    continue
                target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
                extracted = archive.extractfile(member)
                if extracted is None:
                    raise RuntimeError("source archive file cannot be read")
                content = extracted.read(member.size + 1)
                if len(content) != member.size:
                    raise RuntimeError("source archive file is truncated")
                mode = 0o755 if member.mode & 0o111 else 0o644
                _write_exclusive(target, content, mode)
                os.utime(
                    target,
                    (member.mtime, member.mtime),
                    follow_symlinks=False,
                )
        except BaseException:
            _remove_partial_tree(output)
            raise


def _write_exclusive(path: Path, payload: bytes, mode: int) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
        mode,
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _remove_partial_tree(root: Path) -> None:
    for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if path.is_dir() and not path.is_symlink():
            path.rmdir()
        else:
            path.unlink()
    root.rmdir()


def install(output: Path) -> None:
    payload = _download(SOURCE_URL, MAX_DOWNLOAD_BYTES)
    _verify(payload)
    output.mkdir(mode=0o755, parents=True, exist_ok=False)
    try:
        extract_source(payload, output / "source")
        documentation = output / "share" / "doc" / "libarchive"
        documentation.mkdir(mode=0o755, parents=True)
        _write_exclusive(
            documentation / "COPYING",
            (output / "source" / "COPYING").read_bytes(),
            0o644,
        )
        _write_exclusive(documentation / SOURCE_FILENAME, payload, 0o644)
    except BaseException:
        _remove_partial_tree(output)
        raise


def verify_library(path: Path) -> None:
    """Exercise the exact runtime ABI and the closed reader allowlist."""
    library = ctypes.CDLL(str(path), mode=os.RTLD_NOW | os.RTLD_LOCAL)
    library.archive_version_string.restype = ctypes.c_char_p
    if library.archive_version_string() != EXPECTED_VERSION:
        raise RuntimeError("installed libarchive version mismatch")
    library.archive_read_new.restype = ctypes.c_void_p
    reader = library.archive_read_new()
    if not reader:
        raise RuntimeError("installed libarchive reader allocation failed")
    library.archive_read_free.argtypes = [ctypes.c_void_p]
    library.archive_read_free.restype = ctypes.c_int
    try:
        for name in [
            "archive_read_support_filter_gzip",
            "archive_read_support_format_rar",
            "archive_read_support_format_rar5",
            "archive_read_support_format_7zip",
            "archive_read_support_format_zip",
            "archive_read_support_format_tar",
        ]:
            register = getattr(library, name)
            register.argtypes = [ctypes.c_void_p]
            register.restype = ctypes.c_int
            if register(reader) != 0:
                raise RuntimeError(f"installed libarchive cannot register {name}")
    finally:
        if library.archive_read_free(reader) != 0:
            raise RuntimeError("installed libarchive reader cleanup failed")


def main() -> None:
    parser = argparse.ArgumentParser()
    operation = parser.add_mutually_exclusive_group(required=True)
    operation.add_argument("--output", type=Path)
    operation.add_argument("--verify-library", type=Path)
    arguments = parser.parse_args()
    if arguments.output is not None:
        install(arguments.output)
    else:
        verify_library(arguments.verify_library)


if __name__ == "__main__":
    main()
