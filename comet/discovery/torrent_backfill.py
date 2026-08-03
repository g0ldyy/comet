"""One-way migration of credential-independent legacy torrent cache rows."""

from dataclasses import dataclass

from sqlalchemy.engine.url import make_url

from comet.core.locator_codec import policy_json
from comet.core.sources import (
    TORRENT_PROVIDER_KINDS,
    LocatorKind,
    LocatorPolicy,
    TorrentLocator,
)
from comet.discovery.repository import LEGACY_TORRENT_DISCOVERY_CONFIGURATION_ID
from comet.discovery.torrent_repository import (
    ACCOUNT_TRACKER_PREFIX,
    TorrentReleaseRepository,
)

_SQLITE_BATCH_SIZE = 5_000
_POSTGRES_GROUP_BATCH_SIZE = 25_000
_PUBLIC_PARTITION = "0" * 64
_TORRENT_POLICY_JSON = policy_json(
    TorrentLocator(
        locator_id="migration",
        kind=LocatorKind.TORRENT,
        policy=LocatorPolicy(TORRENT_PROVIDER_KINDS),
        info_hash="",
    )
)

_POSTGRES_PUBLIC_PREDICATE = """
    (tracker IS NULL OR substr(tracker, 1, 14) <> :account_prefix)
"""
_POSTGRES_AFTER_CURSOR = """
    (
        CAST(:cursor_media_id AS TEXT) IS NULL
        OR (media_id, info_hash) > (:cursor_media_id, :cursor_info_hash)
    )
"""
_POSTGRES_RANGE_PREDICATE = f"""
    {_POSTGRES_PUBLIC_PREDICATE}
    AND {_POSTGRES_AFTER_CURSOR}
    AND (media_id, info_hash) <= (:end_media_id, :end_info_hash)
"""

_POSTGRES_BOUNDARY_SQL = f"""
    WITH groups AS MATERIALIZED (
        SELECT media_id, info_hash, COUNT(*)::bigint AS row_count
        FROM torrents
        WHERE {_POSTGRES_PUBLIC_PREDICATE}
          AND {_POSTGRES_AFTER_CURSOR}
        GROUP BY media_id, info_hash
        ORDER BY media_id, info_hash
        LIMIT :group_batch_size
    )
    SELECT media_id, info_hash,
           (SELECT SUM(row_count) FROM groups) AS row_count
    FROM groups
    ORDER BY media_id DESC, info_hash DESC
    LIMIT 1
"""

_POSTGRES_IMPORT_SQL = f"""
    WITH legacy AS MATERIALIZED (
        SELECT media_id, info_hash, season_norm, episode_norm,
               file_index, title, seeders, size, tracker,
               sources_json, parsed_json,
               CAST(updated_at AS DOUBLE PRECISION) AS updated_at
        FROM torrents
        WHERE {_POSTGRES_RANGE_PREDICATE}
    ),
    grouped AS (
        SELECT
            legacy.media_id,
            legacy.info_hash,
            CASE
                WHEN BOOL_OR(
                    legacy.season_norm >= 0 OR legacy.episode_norm >= 0
                ) THEN 'series_pack'
                ELSE 'movie'
            END AS scope,
            FLOOR(MAX(legacy.updated_at) * 1000)::bigint AS now_ms,
            MAX(legacy.seeders) AS seeders
        FROM legacy
        GROUP BY legacy.media_id, legacy.info_hash
    ),
    source_documents AS (
        SELECT DISTINCT media_id, info_hash, sources_json
        FROM legacy
    ),
    grouped_sources AS (
        SELECT
            source_documents.media_id,
            source_documents.info_hash,
            JSONB_AGG(
                DISTINCT tracker_source.value ORDER BY tracker_source.value
            ) AS tracker_sources
        FROM source_documents
        CROSS JOIN LATERAL JSONB_ARRAY_ELEMENTS_TEXT(
            source_documents.sources_json::jsonb
        ) AS tracker_source(value)
        GROUP BY source_documents.media_id, source_documents.info_hash
    ),
    representatives AS (
        SELECT DISTINCT ON (media_id, info_hash)
               media_id, info_hash, title, size, tracker
        FROM legacy
        ORDER BY
            media_id,
            info_hash,
            (season_norm = -1 AND episode_norm = -1) DESC,
            size DESC NULLS LAST,
            title DESC
    ),
    candidate_rows AS (
        SELECT
            MD5(
                CONVERT_TO('comet-legacy-candidate-v1', 'UTF8')
                || DECODE('00', 'hex')
                || CONVERT_TO(grouped.media_id, 'UTF8')
                || DECODE('00', 'hex')
                || CONVERT_TO(grouped.info_hash, 'UTF8')
                || DECODE('00', 'hex')
                || CONVERT_TO(grouped.scope, 'UTF8')
            ) AS candidate_digest,
            grouped.media_id,
            'btih:' || grouped.info_hash AS release_key,
            grouped.scope,
            representatives.title,
            representatives.size,
            '{{}}' AS parsed_json,
            JSONB_BUILD_OBJECT(
                'source', COALESCE(
                    NULLIF(representatives.tracker, ''),
                    'Torrent cache'
                ),
                'transport_stats',
                    JSONB_STRIP_NULLS(
                        JSONB_BUILD_OBJECT('seeders', grouped.seeders)
                    )
                    || CASE
                        WHEN grouped_sources.tracker_sources IS NULL
                        THEN '{{}}'::jsonb
                        ELSE JSONB_BUILD_OBJECT(
                            'tracker_sources', grouped_sources.tracker_sources
                        )
                    END
            )::text AS attributes_json,
            grouped.now_ms
        FROM grouped
        JOIN representatives USING (media_id, info_hash)
        LEFT JOIN grouped_sources USING (media_id, info_hash)
    ),
    upserted_candidates AS (
        INSERT INTO release_candidates (
            candidate_id, visibility_partition, media_id,
            transport, release_key, scope, season_norm,
            episode_norm, daily_date, title, byte_size,
            published_at_ms, parsed_json, attributes_json,
            created_at_ms, updated_at_ms, last_seen_at_ms
        )
        SELECT
            SUBSTR(candidate_digest, 1, 8) || '-'
                || SUBSTR(candidate_digest, 9, 4) || '-'
                || SUBSTR(candidate_digest, 13, 4) || '-'
                || SUBSTR(candidate_digest, 17, 4) || '-'
                || SUBSTR(candidate_digest, 21, 12),
            :public_partition,
            media_id,
            'bittorrent',
            release_key,
            scope,
            -1,
            -1,
            NULL,
            title,
            size,
            NULL,
            parsed_json,
            attributes_json,
            now_ms,
            now_ms,
            now_ms
        FROM candidate_rows
        ON CONFLICT (
            visibility_partition, media_id, transport,
            release_key, scope, season_norm, episode_norm
        ) DO UPDATE SET
            daily_date = EXCLUDED.daily_date,
            title = EXCLUDED.title,
            byte_size = EXCLUDED.byte_size,
            published_at_ms = EXCLUDED.published_at_ms,
            parsed_json = EXCLUDED.parsed_json,
            attributes_json = EXCLUDED.attributes_json,
            updated_at_ms = EXCLUDED.updated_at_ms,
            last_seen_at_ms = EXCLUDED.last_seen_at_ms
        RETURNING candidate_id, media_id, release_key, scope
    ),
    upserted_identities AS (
        INSERT INTO candidate_identities (
            candidate_id, visibility_partition, transport,
            identity_scheme, identity_value, created_at_ms, updated_at_ms
        )
        SELECT
            candidate.candidate_id,
            :public_partition,
            'bittorrent',
            'btih',
            grouped.info_hash,
            grouped.now_ms,
            grouped.now_ms
        FROM grouped
        JOIN upserted_candidates AS candidate
          ON candidate.media_id = grouped.media_id
         AND candidate.release_key = 'btih:' || grouped.info_hash
         AND candidate.scope = grouped.scope
        ON CONFLICT (candidate_id, identity_scheme, identity_value)
        DO UPDATE SET updated_at_ms = EXCLUDED.updated_at_ms
    ),
    encoded_locators AS (
        SELECT
            candidate.candidate_id,
            '{{"episode_norm":' || legacy.episode_norm::text
                || ',"file_index":' || COALESCE(legacy.file_index::text, 'null')
                || ',"info_hash":' || TO_JSON(legacy.info_hash)::text
                || ',"season_norm":' || legacy.season_norm::text
                || ',"selection_parsed_json":'
                || TO_JSON(legacy.parsed_json)::text
                || ',"selection_size":' || COALESCE(legacy.size::text, 'null')
                || ',"selection_title":' || TO_JSON(legacy.title)::text
                || '}}' AS locator_json,
            FLOOR(legacy.updated_at * 1000)::bigint AS now_ms
        FROM legacy
        JOIN grouped USING (media_id, info_hash)
        JOIN upserted_candidates AS candidate
          ON candidate.media_id = legacy.media_id
         AND candidate.release_key = 'btih:' || legacy.info_hash
         AND candidate.scope = grouped.scope
    ),
    keyed_locators AS (
        SELECT
            encoded_locators.*,
            ENCODE(
                SHA256(
                    CONVERT_TO('comet-release-locator-v1', 'UTF8')
                    || DECODE('00', 'hex')
                    || CONVERT_TO(:configuration_id, 'UTF8')
                    || DECODE('00', 'hex')
                    || CONVERT_TO('torrent', 'UTF8')
                    || DECODE('00', 'hex')
                    || CONVERT_TO(locator_json, 'UTF8')
                ),
                'hex'
            ) AS content_key
        FROM encoded_locators
    ),
    deduplicated_locators AS (
        SELECT DISTINCT ON (candidate_id, content_key)
               candidate_id, locator_json, content_key, now_ms
        FROM keyed_locators
        ORDER BY candidate_id, content_key, now_ms DESC
    ),
    locator_rows AS (
        SELECT
            MD5(
                CONVERT_TO('comet-legacy-locator-v1', 'UTF8')
                || DECODE('00', 'hex')
                || CONVERT_TO(candidate_id, 'UTF8')
                || DECODE('00', 'hex')
                || CONVERT_TO(content_key, 'UTF8')
            ) AS locator_digest,
            deduplicated_locators.*
        FROM deduplicated_locators
    ),
    upserted_locators AS (
        INSERT INTO release_locators (
            locator_id, candidate_id, origin_kind,
            discovery_configuration_id,
            owner_configuration_partition, account_partition,
            locator_kind, locator_json, policy_json,
            content_key, source_expires_at_ms,
            updated_at_ms, tombstoned_at_ms
        )
        SELECT
            SUBSTR(locator_digest, 1, 8) || '-'
                || SUBSTR(locator_digest, 9, 4) || '-'
                || SUBSTR(locator_digest, 13, 4) || '-'
                || SUBSTR(locator_digest, 17, 4) || '-'
                || SUBSTR(locator_digest, 21, 12),
            candidate_id,
            'discovery',
            :configuration_id,
            :public_partition,
            NULL,
            'torrent',
            locator_json,
            :policy_json,
            content_key,
            NULL,
            now_ms,
            NULL
        FROM locator_rows
        ON CONFLICT (
            candidate_id, locator_kind, content_key,
            owner_configuration_partition
        ) DO UPDATE SET
            account_partition = EXCLUDED.account_partition,
            locator_json = EXCLUDED.locator_json,
            policy_json = EXCLUDED.policy_json,
            source_expires_at_ms = EXCLUDED.source_expires_at_ms,
            updated_at_ms = EXCLUDED.updated_at_ms,
            tombstoned_at_ms = NULL
        RETURNING locator_id
    )
    SELECT COUNT(*) AS locator_count
    FROM upserted_locators
"""


@dataclass(frozen=True, slots=True)
class TorrentBackfillResult:
    eligible_rows: int
    persisted_rows: int


async def backfill_legacy_torrents(database) -> TorrentBackfillResult:
    """Backfill every public legacy row through the optimal backend path."""
    backend = make_url(str(database.url)).get_backend_name()
    if backend == "postgresql":
        return await _backfill_postgres(database)
    return await _backfill_sqlite(database)


async def _backfill_postgres(database) -> TorrentBackfillResult:
    cursor: tuple[str, str] | tuple[None, None] = (None, None)
    eligible_rows = 0
    persisted_rows = 0
    while True:
        boundary = await database.fetch_one(
            _POSTGRES_BOUNDARY_SQL,
            {
                "account_prefix": ACCOUNT_TRACKER_PREFIX,
                "cursor_media_id": cursor[0],
                "cursor_info_hash": cursor[1],
                "group_batch_size": _POSTGRES_GROUP_BATCH_SIZE,
            },
            force_primary=True,
        )
        if boundary is None:
            break
        values = {
            "account_prefix": ACCOUNT_TRACKER_PREFIX,
            "cursor_media_id": cursor[0],
            "cursor_info_hash": cursor[1],
            "end_media_id": boundary["media_id"],
            "end_info_hash": boundary["info_hash"],
            "configuration_id": LEGACY_TORRENT_DISCOVERY_CONFIGURATION_ID,
            "public_partition": _PUBLIC_PARTITION,
            "policy_json": _TORRENT_POLICY_JSON,
        }
        async with database.transaction():
            await database.execute("SET LOCAL synchronous_commit = off")
            imported = await database.fetch_one(
                _POSTGRES_IMPORT_SQL,
                values,
                force_primary=True,
            )
        eligible_rows += int(boundary["row_count"])
        persisted_rows += int(imported["locator_count"])
        cursor = boundary["media_id"], boundary["info_hash"]
    return TorrentBackfillResult(eligible_rows, persisted_rows)


async def _backfill_sqlite(database) -> TorrentBackfillResult:
    repository = TorrentReleaseRepository(database)
    cursor = ("", "", -2, -2)
    eligible_rows = 0
    persisted_rows = 0
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
                    media_id, info_hash, season_norm, episode_norm
                  ) > (
                    :cursor_media_id, :cursor_info_hash,
                    :cursor_season, :cursor_episode
                  )
            ORDER BY media_id, info_hash, season_norm, episode_norm
            LIMIT :batch_size
            """,
            {
                "account_prefix": ACCOUNT_TRACKER_PREFIX,
                "cursor_media_id": cursor[0],
                "cursor_info_hash": cursor[1],
                "cursor_season": cursor[2],
                "cursor_episode": cursor[3],
                "batch_size": _SQLITE_BATCH_SIZE,
            },
            force_primary=True,
        )
        if not rows:
            break
        persisted_rows += await repository.persist_rows(rows)
        eligible_rows += len(rows)
        last = rows[-1]
        cursor = (
            last["media_id"],
            last["info_hash"],
            last["season_norm"],
            last["episode_norm"],
        )

    return TorrentBackfillResult(eligible_rows, persisted_rows)
