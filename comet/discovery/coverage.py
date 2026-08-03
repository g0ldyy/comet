"""Persistent per-query discovery branch coverage."""

import hashlib
import time
from dataclasses import dataclass

from comet.core.capability_states import deterministic_cbor
from comet.core.sources import ReleaseScope
from comet.discovery.models import MediaQuery


@dataclass(frozen=True)
class EffectiveSearchCoverage:
    state: str
    query_fingerprint: str
    branch_fingerprint: str
    next_refresh_at: float | None = None


class SearchCoverageRepository:
    def __init__(self, database):
        self._database = database

    async def effective(
        self,
        query: MediaQuery,
        branch_fingerprint: str,
        *,
        now: float | None = None,
    ) -> EffectiveSearchCoverage:
        values = _coverage_values(query, branch_fingerprint)
        observed_at = time.time() if now is None else _timestamp(now)
        row = await self._database.fetch_one(
            """
            SELECT freshness_state, next_refresh_at
            FROM search_coverage
            WHERE query_fingerprint = :query_fingerprint
              AND branch_fingerprint = :branch_fingerprint
            """,
            {
                key: values[key]
                for key in (
                    "query_fingerprint",
                    "branch_fingerprint",
                )
            },
            force_primary=True,
        )
        if row is None:
            return EffectiveSearchCoverage(
                "missing",
                values["query_fingerprint"],
                values["branch_fingerprint"],
            )
        if row["freshness_state"] in {"fresh", "failed_with_results"}:
            if observed_at >= row["next_refresh_at"]:
                state = "stale"
            elif row["freshness_state"] == "fresh":
                state = "fresh"
            else:
                state = "stale_wait"
        else:
            state = "failed" if observed_at >= row["next_refresh_at"] else "failed_wait"
        return EffectiveSearchCoverage(
            state,
            values["query_fingerprint"],
            values["branch_fingerprint"],
            row["next_refresh_at"],
        )

    async def record_success(
        self,
        query: MediaQuery,
        branch_fingerprint: str,
        *,
        next_refresh_at: float,
        now: float | None = None,
    ) -> None:
        values = _coverage_values(query, branch_fingerprint)
        observed_at = time.time() if now is None else _timestamp(now)
        values["next_refresh_at"] = _future_timestamp(
            next_refresh_at,
            observed_at,
        )
        await self._database.execute(
            """
            INSERT INTO search_coverage (
                query_fingerprint, branch_fingerprint, freshness_state,
                next_refresh_at
            ) VALUES (
                :query_fingerprint, :branch_fingerprint, 'fresh',
                :next_refresh_at
            ) ON CONFLICT (
                query_fingerprint, branch_fingerprint
            ) DO UPDATE SET
                freshness_state = 'fresh',
                next_refresh_at = excluded.next_refresh_at
            """,
            values,
            force_primary=True,
        )

    async def record_failure(
        self,
        query: MediaQuery,
        branch_fingerprint: str,
        *,
        next_refresh_at: float,
        now: float | None = None,
    ) -> None:
        values = _coverage_values(query, branch_fingerprint)
        observed_at = time.time() if now is None else _timestamp(now)
        values["next_refresh_at"] = _future_timestamp(
            next_refresh_at,
            observed_at,
        )
        await self._database.execute(
            """
            INSERT INTO search_coverage (
                query_fingerprint, branch_fingerprint, freshness_state,
                next_refresh_at
            ) VALUES (
                :query_fingerprint, :branch_fingerprint, 'failed',
                :next_refresh_at
            ) ON CONFLICT (
                query_fingerprint, branch_fingerprint
            ) DO UPDATE SET
                freshness_state = CASE
                    WHEN search_coverage.freshness_state IN (
                        'fresh', 'failed_with_results'
                    )
                    THEN 'failed_with_results'
                    ELSE 'failed'
                END,
                next_refresh_at = excluded.next_refresh_at
            """,
            values,
            force_primary=True,
        )


def query_fingerprint(query: MediaQuery) -> str:
    values = _query_values(query)
    return hashlib.sha256(
        b"comet-discovery-query-v2\0"
        + deterministic_cbor(
            [
                values["media_id"],
                values["scope"],
                query.media_type,
                query.season,
                query.episode,
                list(query.title_aliases),
                query.year,
                query.air_date,
                query.absolute_episode,
                query.requested_language,
                query.normalization_fingerprint,
            ]
        )
    ).hexdigest()


def search_scope(query: MediaQuery) -> ReleaseScope:
    return query.scope


def _coverage_values(
    query: MediaQuery,
    branch_fingerprint: str,
) -> dict[str, object]:
    return {
        "query_fingerprint": query_fingerprint(query),
        "branch_fingerprint": branch_fingerprint,
    }


def _query_values(query: MediaQuery) -> dict[str, object]:
    scope = search_scope(query)
    return {
        "media_id": query.media_id,
        "scope": scope,
        "season_norm": -1 if query.season is None else query.season,
        "episode_norm": -1 if query.episode is None else query.episode,
    }


def _timestamp(value: object) -> float:
    return float(value)


def _future_timestamp(value: object, now: float) -> float:
    del now
    return _timestamp(value)
