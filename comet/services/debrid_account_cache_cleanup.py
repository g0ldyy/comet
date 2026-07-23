from dataclasses import dataclass

import orjson

from comet.core.database import (
    build_json_list_membership_predicate,
    database,
    encode_json_param,
)
from comet.core.schema_specs import DEBRID_ACCOUNT_TRACKER_PREDICATE
from comet.services.filtering import TitleMatcher

DEFAULT_CLEANUP_BATCH_SIZE = 1000
_DELETE_BATCH_SIZE = 4000
_TORRENT_HASH_MEMBERSHIP_SQL = build_json_list_membership_predicate(
    "info_hash", "info_hashes"
)


@dataclass(slots=True)
class DebridAccountCleanupStats:
    scanned_hashes: int = 0
    matched_hashes: int = 0
    invalid_hashes: int = 0
    invalid_rows: int = 0
    unverifiable_hashes: int = 0


def _load_title_match_data(value) -> tuple[str, int | None] | None:
    try:
        payload = orjson.loads(value)
    except (TypeError, orjson.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None

    parsed_title = payload.get("parsed_title")
    year = payload.get("year")
    if not isinstance(parsed_title, str) or not parsed_title:
        return None
    if year is not None and (type(year) is not int or year < 0):
        return None
    return parsed_title, year


async def _delete_debrid_account_torrent_hashes(
    media_id: str, info_hashes: set[str] | list[str] | tuple[str, ...]
) -> None:
    unique_hashes = tuple(dict.fromkeys(info_hashes))
    for start in range(0, len(unique_hashes), _DELETE_BATCH_SIZE):
        chunk = unique_hashes[start : start + _DELETE_BATCH_SIZE]
        await database.execute(
            f"""
            DELETE FROM torrents
            WHERE media_id = :media_id
              AND {DEBRID_ACCOUNT_TRACKER_PREDICATE}
              AND {_TORRENT_HASH_MEMBERSHIP_SQL}
            """,
            {
                "media_id": media_id,
                "info_hashes": encode_json_param(chunk),
            },
        )


async def _fetch_hash_batch(media_id: str, after_info_hash: str, batch_size: int):
    return await database.fetch_all(
        f"""
        WITH candidate_hashes AS (
            SELECT DISTINCT info_hash
            FROM torrents
            WHERE media_id = :media_id
              AND {DEBRID_ACCOUNT_TRACKER_PREDICATE}
              AND info_hash > :after_info_hash
            ORDER BY info_hash
            LIMIT :batch_size
        )
        SELECT
            info_hash,
            title,
            parsed_json,
            COUNT(*) AS scope_rows
        FROM torrents
        WHERE media_id = :media_id
          AND {DEBRID_ACCOUNT_TRACKER_PREDICATE}
          AND info_hash IN (SELECT info_hash FROM candidate_hashes)
        GROUP BY info_hash, title, parsed_json
        """,
        {
            "media_id": media_id,
            "after_info_hash": after_info_hash,
            "batch_size": batch_size,
        },
        force_primary=True,
    )


async def repair_debrid_account_cache_for_media(
    *,
    media_id: str,
    media_type: str,
    title: str,
    year: int | None,
    year_end: int | None,
    aliases: dict | None,
    apply: bool,
    batch_size: int = DEFAULT_CLEANUP_BATCH_SIZE,
) -> DebridAccountCleanupStats:
    """Revalidate account-derived cache associations for one media item.

    A hash is retained when any of its persisted titles still passes the current
    title/year filter. Deletion is restricted to DebridAccount provenance, so a
    matching hash discovered independently by another scraper is untouched.
    """

    stats = DebridAccountCleanupStats()
    after_info_hash = ""
    batch_size = max(1, batch_size)
    matcher = TitleMatcher(title, year, year_end, media_type, aliases)

    while rows := await _fetch_hash_batch(media_id, after_info_hash, batch_size):
        selected_hashes = set()
        matched_hashes = set()
        unverifiable_hashes = set()
        row_counts = {}
        for row in rows:
            info_hash = row["info_hash"]
            selected_hashes.add(info_hash)
            row_counts[info_hash] = row_counts.get(info_hash, 0) + int(
                row["scope_rows"]
            )
            if info_hash in matched_hashes:
                continue
            match_data = _load_title_match_data(row["parsed_json"])
            if match_data is None:
                unverifiable_hashes.add(info_hash)
                continue
            if matcher.matches(row["title"], *match_data):
                matched_hashes.add(info_hash)
                unverifiable_hashes.discard(info_hash)

        # A corrupt legacy payload cannot prove a mismatch; retain its hash unless
        # another persisted variant already validates it.
        invalid_hashes = selected_hashes - matched_hashes - unverifiable_hashes

        stats.scanned_hashes += len(selected_hashes)
        stats.matched_hashes += len(matched_hashes)
        stats.invalid_hashes += len(invalid_hashes)
        stats.invalid_rows += sum(row_counts[value] for value in invalid_hashes)
        stats.unverifiable_hashes += len(unverifiable_hashes)
        if apply and invalid_hashes:
            await _delete_debrid_account_torrent_hashes(media_id, invalid_hashes)

        after_info_hash = max(selected_hashes)

    return stats
