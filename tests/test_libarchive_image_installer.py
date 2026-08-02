import io
import tarfile

import pytest

from deployment import install_libarchive


def _source_archive(
    *,
    extra_name: str | None = None,
    extra_type: bytes | None = None,
) -> bytes:
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w:xz") as archive:
        root = tarfile.TarInfo(install_libarchive.SOURCE_ROOT)
        root.type = tarfile.DIRTYPE
        archive.addfile(root)
        for name, content, mode in [
            ("configure", b"#!/bin/sh\n", 0o755),
            ("COPYING", b"BSD license\n", 0o644),
        ]:
            member = tarfile.TarInfo(f"{install_libarchive.SOURCE_ROOT}/{name}")
            member.size = len(content)
            member.mode = mode
            archive.addfile(member, io.BytesIO(content))
        if extra_name is not None:
            member = tarfile.TarInfo(extra_name)
            member.type = extra_type or tarfile.REGTYPE
            content = b"x"
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))
    return payload.getvalue()


def test_extracts_only_the_expected_regular_source_tree(tmp_path):
    output = tmp_path / "source"

    install_libarchive.extract_source(_source_archive(), output)

    assert (output / "configure").read_bytes() == b"#!/bin/sh\n"
    assert (output / "configure").stat().st_mode & 0o777 == 0o755
    assert (output / "COPYING").read_bytes() == b"BSD license\n"


@pytest.mark.parametrize(
    ("name", "kind"),
    [
        ("../outside", None),
        ("/absolute", None),
        (f"{install_libarchive.SOURCE_ROOT}/../outside", None),
        (f"{install_libarchive.SOURCE_ROOT}/linked", tarfile.SYMTYPE),
    ],
)
def test_rejects_unsafe_or_special_members_without_leaving_a_tree(tmp_path, name, kind):
    output = tmp_path / "source"

    with pytest.raises(RuntimeError):
        install_libarchive.extract_source(
            _source_archive(extra_name=name, extra_type=kind),
            output,
        )

    assert not output.exists()
