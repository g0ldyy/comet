from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class SearchParams:
    season: Optional[int]
    episode: Optional[int]


def normalize_search_params(
    season: Optional[int],
    episode: Optional[int],
    search_season: Optional[int] = None,
    search_episode: Optional[int] = None,
) -> SearchParams:
    return SearchParams(
        season=search_season if search_season is not None else season,
        episode=search_episode if search_episode is not None else episode,
    )


def build_torrent_cache_where(
    media_id: str, season: Optional[int], episode: Optional[int]
) -> tuple[str, dict]:
    where_clause = """
        FROM torrents
        WHERE media_id = :media_id
        AND ((season IS NOT NULL AND season = CAST(:season as INTEGER))
             OR (season IS NULL AND CAST(:season as INTEGER) IS NULL))
        AND (episode IS NULL OR episode = CAST(:episode as INTEGER))
    """
    params = {"media_id": media_id, "season": season, "episode": episode}
    return where_clause, params
