import pytest

from comet.usenet.archive_paths import normalize_archive_relative_path


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Season 01\\Episode.mkv", "Season 01/Episode.mkv"),
        (" leading/Movie.mkv", " leading/Movie.mkv"),
        ("Movie.mkv ", None),
        ("../Movie.mkv", None),
        ("CON.txt", None),
        ("Movie.mkv:stream", None),
        ("\ud800.mkv", None),
    ],
)
def test_archive_relative_paths_are_canonical_and_fail_closed(raw, expected):
    assert normalize_archive_relative_path(raw) == expected
