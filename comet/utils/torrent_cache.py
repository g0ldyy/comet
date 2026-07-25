from dataclasses import dataclass

from comet.utils.parsing import MediaScope


@dataclass(frozen=True)
class SearchParams:
    season: int | None
    episode: int | None


def normalize_search_params(
    season: int | None,
    episode: int | None,
    search_season: int | None = None,
    search_episode: int | None = None,
) -> SearchParams:
    return SearchParams(
        season=search_season if search_season is not None else season,
        episode=search_episode if search_episode is not None else episode,
    )


def build_torrent_cache_where(
    media_id: str,
    media_scope: MediaScope,
    season: int | None,
    episode: int | None,
) -> tuple[str, dict]:
    where_clause = """
        FROM torrents
        WHERE media_id = :media_id
    """
    params = {"media_id": media_id}
    if media_scope is not MediaScope.SERIES and season is not None:
        where_clause += """
        AND season = CAST(:season as INTEGER)
        """
        params["season"] = season
    if media_scope in (MediaScope.MOVIE, MediaScope.EPISODE):
        where_clause += """
        AND (episode IS NULL OR episode = CAST(:episode as INTEGER))
        """
        params["episode"] = episode
    return where_clause, params
