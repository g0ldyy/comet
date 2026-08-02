#!/usr/bin/env python3
"""Install the checksum-pinned PAR2 calculator during the container build."""

from __future__ import annotations

import argparse
import hashlib
import io
import os
import stat
import subprocess
import tarfile
import urllib.request
import zipfile
from pathlib import Path

VERSION = "1.4.0"
COMMIT = "4db49ca45ab258c230061fb3f0d29273f7c524ea"
EXPECTED_VERSION = f"par2cmdline-turbo version {VERSION}"
RELEASE_BASE_URL = (
    f"https://github.com/animetosho/par2cmdline-turbo/releases/download/v{VERSION}"
)
RELEASES = {
    "amd64": (
        f"par2cmdline-turbo-{VERSION}-linux-amd64.zip",
        "0be495172b4b8aeabda39c493e47de652813fab88ae745c8633e901c05494281",
    ),
    "arm64": (
        f"par2cmdline-turbo-{VERSION}-linux-arm64.zip",
        "1bb2acb2c549bb3a2e91be3ac6291b00d4b657a56ab23f763f2161ffe7df0fcd",
    ),
}
SOURCE_URL = f"https://github.com/animetosho/par2cmdline-turbo/archive/{COMMIT}.tar.gz"
SOURCE_SHA256 = "34b15e0bc763259f7f3580aab96c6c967e1fe5c077c5da09f69d715120e0ee89"
MAX_RELEASE_BYTES = 8 * 1024 * 1024
MAX_SOURCE_BYTES = 16 * 1024 * 1024
MAX_EXECUTABLE_BYTES = 16 * 1024 * 1024


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


def _verify(payload: bytes, expected_sha256: str, label: str) -> None:
    if not hashlib.sha256(payload).hexdigest() == expected_sha256:
        raise RuntimeError(f"{label} SHA-256 mismatch")


def extract_release(payload: bytes) -> bytes:
    """Return the sole allowlisted executable from a release archive."""
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        members = archive.infolist()
        if len(members) != 1 or members[0].filename != "par2":
            raise RuntimeError("release archive executable allowlist mismatch")
        member = members[0]
        mode = member.external_attr >> 16
        if (
            member.is_dir()
            or member.flag_bits & 0x1
            or stat.S_IFMT(mode) != stat.S_IFREG
            or not mode & 0o111
            or member.file_size > MAX_EXECUTABLE_BYTES
        ):
            raise RuntimeError("release archive contains an invalid executable")
        executable = archive.read(member)
    if len(executable) != member.file_size:
        raise RuntimeError("release executable is truncated")
    return executable


def _source_member(payload: bytes, relative_name: str) -> bytes:
    root = f"par2cmdline-turbo-{COMMIT}"
    expected_name = f"{root}/{relative_name}"
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        matches = [
            member for member in archive.getmembers() if member.name == expected_name
        ]
        if (
            len(matches) != 1
            or not matches[0].isfile()
            or matches[0].size > 1024 * 1024
        ):
            raise RuntimeError(f"source archive lacks safe {relative_name}")
        extracted = archive.extractfile(matches[0])
        if extracted is None:
            raise RuntimeError(f"source archive lacks {relative_name}")
        content = extracted.read(matches[0].size + 1)
    if len(content) != matches[0].size:
        raise RuntimeError(f"source archive has invalid {relative_name}")
    return content


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


def install(architecture: str, output: Path) -> None:
    try:
        filename, expected_release_sha256 = RELEASES[architecture]
    except KeyError as error:
        raise RuntimeError(
            f"unsupported target architecture: {architecture}"
        ) from error

    release = _download(f"{RELEASE_BASE_URL}/{filename}", MAX_RELEASE_BYTES)
    _verify(release, expected_release_sha256, "release archive")
    executable = extract_release(release)

    source = _download(SOURCE_URL, MAX_SOURCE_BYTES)
    _verify(source, SOURCE_SHA256, "corresponding source archive")
    copying = _source_member(source, "COPYING")
    authors = _source_member(source, "AUTHORS")

    output.mkdir(mode=0o755, parents=True, exist_ok=False)
    bin_directory = output / "bin"
    documentation = output / "share" / "doc" / "par2cmdline-turbo"
    bin_directory.mkdir(mode=0o755)
    documentation.mkdir(mode=0o755, parents=True)
    executable_path = bin_directory / "par2"
    _write_exclusive(executable_path, executable, 0o755)
    _write_exclusive(documentation / "COPYING", copying, 0o644)
    _write_exclusive(documentation / "AUTHORS", authors, 0o644)
    _write_exclusive(documentation / "source.tar.gz", source, 0o644)

    completed = subprocess.run(
        [executable_path, "-V"],
        check=False,
        close_fds=True,
        capture_output=True,
        env={"LANG": "C", "LC_ALL": "C"},
        text=True,
        timeout=5,
    )
    if (
        completed.returncode != 0
        or completed.stdout.strip() != EXPECTED_VERSION
        or completed.stderr
    ):
        raise RuntimeError("installed calculator failed its exact version check")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--arch", required=True, choices=sorted(RELEASES))
    parser.add_argument("--output", required=True, type=Path)
    arguments = parser.parse_args()
    install(arguments.arch, arguments.output)


if __name__ == "__main__":
    main()
