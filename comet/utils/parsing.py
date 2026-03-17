from functools import lru_cache

from RTN import ParsedData

SCRAPE_URL_MODE_BOTH = "both"
SCRAPE_URL_MODES = frozenset((SCRAPE_URL_MODE_BOTH, "live", "background"))


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
    return title.endswith(video_extensions)


def default_dump(obj):
    if isinstance(obj, ParsedData):
        return obj.model_dump()


def parse_optional_int(value: str | None):
    if value == "n" or value is None or value == "":
        return None
    try:
        return int(value)
    except ValueError:
        return None


def parse_media_id(media_type: str, media_id: str):
    if media_id.startswith("kitsu:"):
        _, _, rest = media_id.partition(":")
        kitsu_id, _, episode_str = rest.partition(":")
        return kitsu_id, 1, parse_optional_int(episode_str) if episode_str else None
    if media_type == "series":
        series_id, sep1, rest1 = media_id.partition(":")
        if not sep1:
            return series_id, None, None
        season_str, sep2, episode_str = rest1.partition(":")
        return (
            series_id,
            parse_optional_int(season_str),
            parse_optional_int(episode_str) if sep2 else None,
        )

    return media_id, None, None


def match_parsed_episode_target(
    parsed: ParsedData,
    season: int | None,
    episode: int | None,
    target_air_date: str | None = None,
    reject_unknown_episode_files: bool = False,
) -> bool:
    parsed_seasons = parsed.seasons

    if episode is None:
        if parsed.episodes:
            if len(parsed.episodes) == 1:
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
    if not urls:
        return []

    if isinstance(urls, str):
        urls = [urls]

    if len(urls) == 1:
        if credentials is None:
            credential = None
        elif isinstance(credentials, str):
            credential = credentials or None
        elif isinstance(credentials, list) and len(credentials) > 0:
            credential = credentials[0]
        else:
            credential = None

        credentials_list = [credential]
    else:
        if credentials is None:
            credentials_list = [None] * len(urls)
        elif isinstance(credentials, str):
            credentials_list = [credentials or None] * len(urls)
        elif isinstance(credentials, list):
            credentials_list = []
            for i in range(len(urls)):
                if i < len(credentials):
                    cred = credentials[i] or None
                    credentials_list.append(cred)
                else:
                    credentials_list.append(None)

    return list(zip(urls, credentials_list))
