"""
Changelog validation tests focused on the 2.11.0 release added in this PR.

Testing library and framework:
- pytest (assert-only style). We intentionally avoid pytest-specific fixtures so the
  tests remain simple and readable. Run with: `pytest -q`.

Scope and intent:
- Validate the latest release section "2.11.0 (2025-08-23)" exists and is well-formed.
- Verify the compare link, section headings, bullet formatting, and expected commit links.
- Provide additional structural checks (SemVer headers) for resilience.

If your project uses a different runner, these tests still work under pytest without extra deps.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import List, Tuple

EXPECTED_VERSION = "2.11.0"
EXPECTED_DATE = "2025-08-23"
EXPECTED_COMPARE_URL = "https://github.com/g0ldyy/comet/compare/v2.10.0...v2.11.0"
EXPECTED_REPO_PREFIX = "https://github.com/g0ldyy/comet"

# Expected items introduced in the diff
EXPECTED_FEATURE_ENTRY = "implement new admin dashboard"
EXPECTED_FEATURE_COMMIT_SHA = "dfe00a280c37686df9cc0ad5a1bc9e0d32fcc125"

EXPECTED_BUGFIX_ENTRY = "use torrent name as media_id instead of torrent hash"
EXPECTED_BUGFIX_COMMIT_SHA = "740140eb2748fa1ccde70c1fea39f6add5d03c6f"


def _repo_root() -> Path:
    # tests/<this file> -> repo root
    return Path(__file__).resolve().parents[1]


def _find_changelog() -> Path:
    root = _repo_root()
    candidates = [
        root / "CHANGELOG.md",
        root / "Changelog.md",
        root / "CHANGELOG",
        root / "docs" / "CHANGELOG.md",
        root / "docs" / "Changelog.md",
    ]
    for c in candidates:
        if c.exists():
            return c

    # Fallback to a recursive search for any CHANGELOG-like file
    matches: List[Path] = []
    for pat in ("CHANGELOG*.md", "Changelog*.md", "CHANGELOG*"):
        matches.extend(root.rglob(pat))
    if matches:
        # Prefer the most canonical-looking name
        matches.sort(key=lambda p: (len(p.name), str(p)))
        return matches[0]

    raise AssertionError(
        "CHANGELOG file not found. Expected one of: CHANGELOG.md, Changelog.md, docs/CHANGELOG.md, etc."
    )


def _read_changelog_text() -> str:
    p = _find_changelog()
    return p.read_text(encoding="utf-8", errors="strict")


def _extract_release_block(text: str, version: str) -> Tuple[str, str]:
    """
    Returns (date, body) for the given version, where:
      - date is YYYY-MM-DD
      - body is the markdown content under the version header, up to the next version header or EOF
    """
    # Header line looks like:
    # ## [2.11.0](https://github.com/g0ldyy/comet/compare/v2.10.0...v2.11.0) (2025-08-23)
    pattern = (
        rf"(?ms)^## \[{re.escape(version)}\]\([^)]+\) "
        rf"\((?P<date>\d{{4}}-\d{{2}}-\d{{2}})\)\s*\n+(?P<body>.*?)(?=^\s*## \[|\Z)"
    )
    m = re.search(pattern, text)
    if not m:
        raise AssertionError(f"Release section for {version} not found in CHANGELOG.")
    return m.group("date"), m.group("body")


def _extract_section_bullets(block: str, section_name: str) -> List[str]:
    # Match a section like:
    # ### Features
    # * item 1
    # * item 2
    sec_pat = rf"(?ms)^### {re.escape(section_name)}\s*\n+(?P<bullets>(?:\* .*(?:\n|$))+)"
    m = re.search(sec_pat, block)
    if not m:
        return []
    bullets_text = m.group("bullets").strip("\n")
    return [line.rstrip() for line in bullets_text.splitlines() if line.strip()]


def _find_commit_links(text: str) -> List[Tuple[str, str]]:
    """
    Returns list of (full_url, sha) for commit links in the given text.
    Looks for markdown links like: ([sha7]) where the URL path ends with /commit/<sha>
    """
    # We accept 7..40 lowercase hex; GitHub full SHAs are 40 chars.
    link_pat = r"\((https://github\.com/[^\s)]+/commit/([0-9a-f]{7,40}))\)"
    return re.findall(link_pat, text)


def _parse_release_headers(text: str) -> List[str]:
    # Collect all SemVer headers e.g., ## [1.2.3](...) (YYYY-MM-DD)
    headers = re.findall(r"(?m)^## \[(\d+\.\d+\.\d+)\]\(", text)
    return headers


def _semver_tuple(v: str) -> Tuple[int, int, int]:
    major, minor, patch = (int(x) for x in v.split("."))
    return major, minor, patch


def test_release_header_contains_expected_version_date_and_compare_url():
    text = _read_changelog_text()

    header_pat = r"(?m)^## \[2\.11\.0\]\((?P<url>[^)]+)\) \((?P<date>\d{4}-\d{2}-\d{2})\)"
    m = re.search(header_pat, text)
    assert m, "Expected header for 2.11.0 not found."

    compare_url = m.group("url")
    date = m.group("date")

    assert compare_url == EXPECTED_COMPARE_URL, f"Unexpected compare URL: {compare_url}"
    assert date == EXPECTED_DATE, f"Unexpected release date: {date}"
    assert compare_url.startswith(EXPECTED_REPO_PREFIX + "/compare/"), "Compare URL should point to the project repo."


def test_release_contains_features_and_bugfixes_sections_with_expected_entries():
    text = _read_changelog_text()
    date, body = _extract_release_block(text, EXPECTED_VERSION)
    assert date == EXPECTED_DATE, f"Release date mismatch for {EXPECTED_VERSION}: {date}"

    features = _extract_section_bullets(body, "Features")
    bugfixes = _extract_section_bullets(body, "Bug Fixes")

    assert features, "Features section missing or empty for 2.11.0."
    assert bugfixes, "Bug Fixes section missing or empty for 2.11.0."

    # Validate specific entries introduced by the diff
    assert any(EXPECTED_FEATURE_ENTRY in line for line in features), \
        f"Expected feature entry not found: {EXPECTED_FEATURE_ENTRY}"
    assert any(EXPECTED_BUGFIX_ENTRY in line for line in bugfixes), \
        f"Expected bug fix entry not found: {EXPECTED_BUGFIX_ENTRY}"

    # Ensure bullets begin with "* "
    assert all(line.startswith("* ") for line in features), "All feature bullets should start with '* '."
    assert all(line.startswith("* ") for line in bugfixes), "All bug fix bullets should start with '* '."

    # Validate commit links for those bullets
    feature_text = "\n".join(features)
    bugfix_text = "\n".join(bugfixes)
    feature_links = _find_commit_links(feature_text)
    bugfix_links = _find_commit_links(bugfix_text)

    assert feature_links, "Expected at least one commit link in Features."
    assert bugfix_links, "Expected at least one commit link in Bug Fixes."

    # Check exact SHAs match what's in the diff
    assert any(sha == EXPECTED_FEATURE_COMMIT_SHA for _, sha in feature_links), \
        f"Expected feature commit SHA not found: {EXPECTED_FEATURE_COMMIT_SHA}"
    assert any(sha == EXPECTED_BUGFIX_COMMIT_SHA for _, sha in bugfix_links), \
        f"Expected bug fix commit SHA not found: {EXPECTED_BUGFIX_COMMIT_SHA}"


def test_commit_links_use_valid_format_and_sha_lengths():
    text = _read_changelog_text()
    _, body = _extract_release_block(text, EXPECTED_VERSION)
    links = _find_commit_links(body)
    assert links, "No commit links found in 2.11.0 release."

    for url, sha in links:
        assert url.startswith(EXPECTED_REPO_PREFIX + "/commit/"), f"Commit URL should point to project repo: {url}"
        assert 7 <= len(sha) <= 40, f"Commit SHA length should be between 7 and 40: {sha}"
        assert re.fullmatch(r"[0-9a-f]{7,40}", sha), f"Commit SHA must be lowercase hex: {sha}"


def test_semver_headers_exist_and_latest_is_2_11_0():
    text = _read_changelog_text()
    versions = _parse_release_headers(text)
    assert versions, "No SemVer release headers found in CHANGELOG."
    assert versions[0] == EXPECTED_VERSION, f"Latest release should be {EXPECTED_VERSION}, found {versions[0]} instead."

    # Optional: if there is a second header, ensure ordering is descending
    if len(versions) > 1:
        latest = _semver_tuple(versions[0])
        next_ver = _semver_tuple(versions[1])
        assert latest >= next_ver, f"Release order should be descending: {versions[0]} >= {versions[1]}"