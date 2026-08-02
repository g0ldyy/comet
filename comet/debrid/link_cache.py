"""Short-lived cache for account-bound provider download links."""

import hashlib
import re
import time
from urllib.parse import urlsplit

from comet.core.database import (
    DOWNLOAD_LINK_CACHE_TTL,
    build_scope_lookup_params,
    build_scope_params,
)
from comet.observability import log

_MALFORMED_PERCENT = re.compile(r"%(?![0-9A-Fa-f]{2})")
_MAX_DOWNLOAD_URL_BYTES = 8 * 1024
_CACHE_DOWNLOAD_LINK_SQL = """
    INSERT INTO download_links_cache (
        debrid_service,
        account_key_hash,
        info_hash,
        season,
        episode,
        season_norm,
        episode_norm,
        selection_key,
        client_scope,
        download_url,
        updated_at
    )
    VALUES (
        :debrid_service,
        :account_key_hash,
        :info_hash,
        :season,
        :episode,
        :season_norm,
        :episode_norm,
        :selection_key,
        :client_scope,
        :download_url,
        :updated_at
    )
    ON CONFLICT (
        debrid_service,
        account_key_hash,
        info_hash,
        season_norm,
        episode_norm,
        selection_key,
        client_scope
    ) DO UPDATE SET
        download_url = EXCLUDED.download_url,
        updated_at = EXCLUDED.updated_at
"""


def valid_download_url(value: object) -> str | None:
    if type(value) is not str or not value:
        return None
    try:
        encoded = value.encode("utf-8")
        parsed = urlsplit(value)
        if (
            not 1 <= len(encoded) <= _MAX_DOWNLOAD_URL_BYTES
            or any(
                character.isspace() or ord(character) < 32 or ord(character) == 127
                for character in value
            )
            or "\\" in value
            or _MALFORMED_PERCENT.search(value)
            or parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or "@" in parsed.netloc
            or parsed.fragment
        ):
            return None
        _ = parsed.port
    except (ValueError, UnicodeError):
        return None
    return value


def _client_scope(client_ip: str) -> str:
    return hashlib.sha256(client_ip.encode()).hexdigest() if client_ip else ""


async def get_cached_download_link(
    database,
    *,
    debrid_service: str,
    account_key_hash: str,
    info_hash: str,
    season: int | None,
    episode: int | None,
    selection_key: str,
    client_ip: str,
) -> str | None:
    row = await database.fetch_one(
        """
        SELECT download_url
        FROM download_links_cache
        WHERE debrid_service = :debrid_service
          AND account_key_hash = :account_key_hash
          AND info_hash = :info_hash
          AND season_norm = :season_norm
          AND episode_norm = :episode_norm
          AND selection_key = :selection_key
          AND client_scope = :client_scope
          AND updated_at >= :min_timestamp
        """,
        {
            "debrid_service": debrid_service,
            "account_key_hash": account_key_hash,
            "info_hash": info_hash,
            "selection_key": selection_key,
            "client_scope": _client_scope(client_ip),
            "min_timestamp": time.time() - DOWNLOAD_LINK_CACHE_TTL,
            **build_scope_lookup_params(season, episode),
        },
    )
    return valid_download_url(row["download_url"]) if row is not None else None


async def cache_download_link_best_effort(
    database,
    *,
    debrid_service: str,
    account_key_hash: str,
    info_hash: str,
    season: int | None,
    episode: int | None,
    selection_key: str,
    client_ip: str,
    download_url: str,
) -> None:
    values = {
        "debrid_service": debrid_service,
        "account_key_hash": account_key_hash,
        "info_hash": info_hash,
        "selection_key": selection_key,
        "client_scope": _client_scope(client_ip),
        "download_url": download_url,
        "updated_at": time.time(),
        **build_scope_params(season, episode),
    }
    try:
        await database.execute(_CACHE_DOWNLOAD_LINK_SQL, values)
    except Exception as error:
        log.warning(
            "debrid.download_link_cache.persist_failed",
            "Debrid download link cache persistence failed",
            debrid_service=debrid_service,
            exc=error,
        )
