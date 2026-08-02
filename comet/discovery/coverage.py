"""Persistent per-query discovery branch coverage."""

import hashlib
import math
import re
import time
from dataclasses import dataclass

from comet.core.capability_states import deterministic_cbor
from comet.core.sources import ReleaseScope
from comet.discovery.models import (
    MAX_TITLE_ALIAS_BYTES,
    MAX_TITLE_ALIASES,
    MediaQuery,
)

_FINGERPRINT_RE = re.compile(r"[0-9a-f]{64}")
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


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
        b"comet-discovery-query-v1\0"
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
            ]
        )
    ).hexdigest()


def search_scope(query: MediaQuery) -> ReleaseScope:
    if not isinstance(query, MediaQuery):
        raise ValueError("media query is invalid")
    return query.scope


def _coverage_values(
    query: MediaQuery,
    branch_fingerprint: str,
) -> dict[str, object]:
    if (
        not isinstance(branch_fingerprint, str)
        or _FINGERPRINT_RE.fullmatch(branch_fingerprint) is None
    ):
        raise ValueError("discovery branch fingerprint is invalid")
    return {
        "query_fingerprint": query_fingerprint(query),
        "branch_fingerprint": branch_fingerprint,
    }


def _query_values(query: MediaQuery) -> dict[str, object]:
    if not isinstance(query, MediaQuery):
        raise ValueError("media query is invalid")
    if (
        not isinstance(query.media_id, str)
        or not 1 <= len(query.media_id) <= 128
        or any(ord(character) < 32 for character in query.media_id)
    ):
        raise ValueError("media query identifier is invalid")
    if (
        not isinstance(query.media_type, str)
        or not 1 <= len(query.media_type) <= 32
        or not all(
            character.isascii() and (character.isalnum() or character in {"_", "-"})
            for character in query.media_type
        )
    ):
        raise ValueError("media query type is invalid")
    scope = search_scope(query)
    for value in (query.season, query.episode, query.absolute_episode):
        if value is not None and (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 0 <= value <= 100_000
        ):
            raise ValueError("media query episode scope is invalid")
    if query.year is not None and (
        isinstance(query.year, bool)
        or not isinstance(query.year, int)
        or not 1800 <= query.year <= 3000
    ):
        raise ValueError("media query year is invalid")
    if query.air_date is not None and (
        not isinstance(query.air_date, str)
        or _DATE_RE.fullmatch(query.air_date) is None
    ):
        raise ValueError("media query air date is invalid")
    if (
        not isinstance(query.title_aliases, tuple)
        or len(query.title_aliases) > MAX_TITLE_ALIASES
        or any(
            not isinstance(alias, str)
            or not 1 <= len(alias.encode("utf-8")) <= MAX_TITLE_ALIAS_BYTES
            or any(ord(character) < 32 for character in alias)
            for alias in query.title_aliases
        )
    ):
        raise ValueError("media query aliases are invalid")
    if query.requested_language is not None and (
        not isinstance(query.requested_language, str)
        or not 1 <= len(query.requested_language) <= 32
        or not all(
            character.isascii() and (character.isalnum() or character in {"_", "-"})
            for character in query.requested_language
        )
    ):
        raise ValueError("media query language is invalid")
    return {
        "media_id": query.media_id,
        "scope": scope,
        "season_norm": -1 if query.season is None else query.season,
        "episode_norm": -1 if query.episode is None else query.episode,
    }


def _timestamp(value: object) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise ValueError("search coverage timestamp is invalid")
    return float(value)


def _future_timestamp(value: object, now: float) -> float:
    future = _timestamp(value)
    if future <= now:
        raise ValueError("search coverage refresh time must be in the future")
    return future
