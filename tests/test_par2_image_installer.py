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


def test_release_pins_are_the_normative_architecture_specific_values():
    assert install_par2.COMMIT == "4db49ca45ab258c230061fb3f0d29273f7c524ea"
    assert install_par2.RELEASES == {
        "amd64": (
            "par2cmdline-turbo-1.4.0-linux-amd64.zip",
            "0be495172b4b8aeabda39c493e47de652813fab88ae745c8633e901c05494281",
        ),
        "arm64": (
            "par2cmdline-turbo-1.4.0-linux-arm64.zip",
            "1bb2acb2c549bb3a2e91be3ac6291b00d4b657a56ab23f763f2161ffe7df0fcd",
        ),
    }
