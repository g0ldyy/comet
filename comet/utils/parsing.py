from functools import lru_cache
import re

import orjson
from RTN import ParsedData

SCRAPE_URL_MODE_BOTH = "both"
SCRAPE_URL_MODES = frozenset((SCRAPE_URL_MODE_BOTH, "live", "background"))
_CANONICAL_NONNEGATIVE_INTEGER = re.compile(r"0|[1-9][0-9]*")
_IMDB_ID = re.compile(r"tt[0-9]{7,10}")
_KITSU_ID = re.compile(r"[1-9][0-9]*")


def load_cached_parsed(value) -> ParsedData | None:
    try:
        payload = orjson.loads(value)
    except (TypeError, orjson.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    try:
        return ParsedData(**payload)
    except ValueError:
        return None


def load_cached_string_list(value) -> list[str]:
    try:
        payload = orjson.loads(value)
    except (TypeError, orjson.JSONDecodeError):
        return []
    if not isinstance(payload, list):
        return []
    return [item for item in payload if isinstance(item, str) and item]


def ensure_multi_language(parsed: ParsedData):
    languages = parsed.languages

    if not (len(languages) > 1 or parsed.dubbed):
        return

    if languages and languages[0] == "multi":
        return

    try:
        languages.remove("multi")
    except ValueError:
        pass

    languages.insert(0, "multi")
    parsed.languages = languages


def is_video(title: str):
    video_extensions = (
        ".3g2",
        ".3gp",
        ".amv",
        ".asf",
        ".avi",
        ".drc",
        ".f4a",
        ".f4b",
        ".f4p",
        ".f4v",
        ".flv",
        ".gif",
        ".gifv",
        ".m2v",
        ".m4p",
        ".m4v",
        ".mkv",
        ".mov",
        ".mp2",
        ".mp4",
        ".mpg",
        ".mpeg",
        ".mpv",
        ".mng",
        ".mpe",
        ".mxf",
        ".nsv",
        ".ogg",
        ".ogv",
        ".qt",
        ".rm",
        ".rmvb",
        ".roq",
        ".svi",
        ".webm",
        ".wmv",
        ".yuv",
    )
    return title.lower().endswith(video_extensions)


def default_dump(obj):
    if isinstance(obj, ParsedData):
        return obj.model_dump()


def parse_optional_int(value: str | None):
    if value == "n" or value is None or value == "":
        return None
    if (
        type(value) is not str
        or _CANONICAL_NONNEGATIVE_INTEGER.fullmatch(value) is None
    ):
        return None
    return int(value)


def parse_media_id(media_type: str, media_id: str):
    if media_type not in {"movie", "series"}:
        raise ValueError("media type must be movie or series")
    if type(media_id) is not str or not media_id:
        raise ValueError("media ID must be a non-empty string")

    if media_id.startswith("kitsu:"):
        parts = media_id.split(":")
        if (
            len(parts) not in {2, 3}
            or _KITSU_ID.fullmatch(parts[1]) is None
            or (len(parts) == 3 and parse_optional_int(parts[2]) is None)
        ):
            raise ValueError("Kitsu media ID has an invalid current shape")
        if media_type == "movie" and len(parts) != 2:
            raise ValueError("movie Kitsu IDs cannot include an episode")
        episode = parse_optional_int(parts[2]) if len(parts) == 3 else None
        return parts[1], 1, episode

    parts = media_id.split(":")
    if _IMDB_ID.fullmatch(parts[0]) is None:
        raise ValueError("IMDb media ID has an invalid current shape")
    if media_type == "series":
        if (
            len(parts) != 3
            or parse_optional_int(parts[1]) is None
            or parse_optional_int(parts[2]) is None
        ):
            raise ValueError("series IMDb ID must include season and episode")
        return parts[0], int(parts[1]), int(parts[2])

    if len(parts) != 1:
        raise ValueError("movie IMDb IDs cannot include episode segments")
    return parts[0], None, None


def match_parsed_episode_target(
    parsed: ParsedData,
    season: int | None,
    episode: int | None,
    target_air_date: str | None = None,
    reject_unknown_episode_files: bool = False,
) -> bool:
    parsed_seasons = parsed.seasons

    if episode is None:
        parsed_episodes = parsed.episodes
        if parsed_episodes and (season is None or len(parsed_episodes) == 1):
            return False
        if season is None:
            return True
        return not parsed_seasons or season in parsed_seasons

    parsed_episodes = parsed.episodes

    if parsed_seasons and season is not None and season not in parsed_seasons:
        return False
    if parsed_episodes and episode not in parsed_episodes:
        return False

    if parsed_seasons or parsed_episodes:
        if reject_unknown_episode_files and (not parsed_episodes or not parsed_seasons):
            return False
        return True

    parsed_date = parsed.date
    if isinstance(parsed_date, str) and parsed_date:
        if target_air_date is None:
            return not reject_unknown_episode_files
        return parsed_date == target_air_date

    parsed_year = parsed.year
    if parsed.complete and parsed_year and target_air_date:
        target_year_str = target_air_date[:4]
        if target_year_str.isdigit():
            return parsed_year == int(target_year_str)

    return not reject_unknown_episode_files


def parsed_matches_target(
    parsed: ParsedData,
    season: int | None,
    episode: int | None,
    target_air_date: str | None = None,
    reject_unknown_episode_files: bool = False,
) -> bool:
    return match_parsed_episode_target(
        parsed,
        season,
        episode,
        target_air_date=target_air_date,
        reject_unknown_episode_files=reject_unknown_episode_files,
    )


@lru_cache(maxsize=1024)
def parse_url_scrape_mode(url: str):
    normalized = url.strip().rstrip("/")
    base_url, separator, mode = normalized.rpartition(":")
    if separator:
        lowered_mode = mode.lower()
        if lowered_mode in SCRAPE_URL_MODES:
            return base_url.rstrip("/"), lowered_mode
    return normalized, SCRAPE_URL_MODE_BOTH


def url_mode_matches_context(mode: str, context: str):
    return mode == SCRAPE_URL_MODE_BOTH or mode == context


def associate_urls_credentials(urls, credentials):
    if urls is None or urls == []:
        return []
    if type(urls) is str:
        if not urls:
            raise ValueError("scraper URL must be non-empty")
        url_list = [urls]
    elif type(urls) is list:
        if any(type(url) is not str or not url for url in urls):
            raise ValueError("scraper URLs must be non-empty strings")
        url_list = urls
    else:
        raise TypeError("scraper URLs must be a string, list, or None")

    if credentials is None:
        credentials_list = [None] * len(url_list)
    elif type(credentials) is str:
        credentials_list = [credentials or None] * len(url_list)
    elif type(credentials) is list:
        if len(credentials) != len(url_list):
            raise ValueError("credential list must match the scraper URL list length")
        if any(type(credential) is not str for credential in credentials):
            raise TypeError("scraper credentials must be strings")
        credentials_list = [credential or None for credential in credentials]
    else:
        raise TypeError("scraper credentials must be a string, list, or None")

    return list(zip(url_list, credentials_list))
