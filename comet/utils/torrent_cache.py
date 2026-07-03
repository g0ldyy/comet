from dataclasses import dataclass
from typing import Optional, Union

from comet.core.database import build_json_list_membership_predicate, encode_json_param


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
    media_id: Union[str, list[str]], season: Optional[int], episode: Optional[int]
) -> tuple[str, dict]:
    params = {"episode": episode}
    if isinstance(media_id, list):
        where_clause = f"""
            FROM torrents
            WHERE {build_json_list_membership_predicate("media_id", "media_ids")}
        """
        params["media_ids"] = encode_json_param(media_id)
    else:
        where_clause = """
            FROM torrents
            WHERE media_id = :media_id
        """
        params["media_id"] = media_id

    if season is not None:
        where_clause += """
        AND season = CAST(:season as INTEGER)
        """
        params["season"] = season
    where_clause += """
        AND (episode IS NULL OR episode = CAST(:episode as INTEGER))
    """
    return where_clause, params
