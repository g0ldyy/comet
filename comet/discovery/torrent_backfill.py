"""One-way migration of credential-independent legacy torrent cache rows."""

from dataclasses import dataclass

from comet.discovery.torrent_repository import (
    ACCOUNT_TRACKER_PREFIX,
    TorrentReleaseRepository,
)

_BATCH_SIZE = 500


@dataclass(frozen=True, slots=True)
class TorrentBackfillResult:
    eligible_rows: int
    persisted_rows: int
    discarded_rows: int


async def backfill_legacy_torrents(database) -> TorrentBackfillResult:
    """Backfill every public legacy row through the generic repository."""
    repository = TorrentReleaseRepository(database)
    cursor = ("", "", -2, -2)
    eligible_rows = 0
    persisted_rows = 0
    discarded_rows = 0
    while True:
        rows = await database.fetch_all(
            """
            SELECT media_id, info_hash, season_norm, episode_norm,
                   file_index, title, seeders, size, tracker,
                   sources_json, parsed_json, updated_at
            FROM torrents
            WHERE (
                    tracker IS NULL
                    OR substr(tracker, 1, 14) <> :account_prefix
                  )
              AND (
                    media_id > :cursor_media_id
                    OR (
                        media_id = :cursor_media_id
                        AND info_hash > :cursor_info_hash
                    )
                    OR (
                        media_id = :cursor_media_id
                        AND info_hash = :cursor_info_hash
                        AND season_norm > :cursor_season
                    )
                    OR (
                        media_id = :cursor_media_id
                        AND info_hash = :cursor_info_hash
                        AND season_norm = :cursor_season
                        AND episode_norm > :cursor_episode
                    )
                  )
            ORDER BY media_id, info_hash, season_norm, episode_norm,
                     COALESCE(updated_at, 0) DESC,
                     COALESCE(seeders, -1) DESC, title DESC
            LIMIT :batch_size
            """,
            {
                "account_prefix": ACCOUNT_TRACKER_PREFIX,
                "cursor_media_id": cursor[0],
                "cursor_info_hash": cursor[1],
                "cursor_season": cursor[2],
                "cursor_episode": cursor[3],
                "batch_size": _BATCH_SIZE,
            },
            force_primary=True,
        )
        if not rows:
            break
        persisted, discarded = await repository.persist_migratable_rows(rows)
        persisted_rows += persisted
        discarded_rows += discarded
        eligible_rows += len(rows) - discarded
        last = rows[-1]
        cursor = (
            last["media_id"],
            last["info_hash"],
            last["season_norm"],
            last["episode_norm"],
        )

    if persisted_rows != eligible_rows:
        raise RuntimeError("legacy torrent backfill invariant failed")
    return TorrentBackfillResult(eligible_rows, persisted_rows, discarded_rows)
