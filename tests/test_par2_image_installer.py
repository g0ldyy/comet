import io
import stat
import zipfile

import pytest

from deployment import install_par2


def _release_archive(
    *,
    name: str = "par2",
    mode: int = stat.S_IFREG | 0o755,
    extra: bool = False,
) -> bytes:
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        member = zipfile.ZipInfo(name)
        member.create_system = 3
        member.external_attr = mode << 16
        archive.writestr(member, b"calculator")
        if extra:
            archive.writestr("unexpected", b"payload")
    return payload.getvalue()


def test_release_archive_accepts_only_the_expected_executable():
    assert install_par2.extract_release(_release_archive()) == b"calculator"

    for archive in (
        _release_archive(name="../par2"),
        _release_archive(mode=stat.S_IFREG | 0o644),
        _release_archive(mode=stat.S_IFLNK | 0o755),
        _release_archive(extra=True),
    ):
        with pytest.raises(RuntimeError):
            install_par2.extract_release(archive)
