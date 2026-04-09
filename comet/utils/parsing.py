from functools import lru_cache

from RTN import ParsedData

SCRAPE_URL_MODE_BOTH = "both"
SCRAPE_URL_MODES = frozenset((SCRAPE_URL_MODE_BOTH, "live", "background"))

STREMTHRU_AUDIO_LANGUAGE_ALIASES = {
    "en": "en",
    "eng": "en",
    "english": "en",
    "ja": "ja",
    "jpn": "ja",
    "japanese": "ja",
    "zh": "zh",
    "zho": "zh",
    "chi": "zh",
    "chinese": "zh",
    "mandarin": "zh",
    "ru": "ru",
    "rus": "ru",
    "russian": "ru",
    "ar": "ar",
    "ara": "ar",
    "arabic": "ar",
    "pt": "pt",
    "por": "pt",
    "portuguese": "pt",
    "es": "es",
    "spa": "es",
    "spanish": "es",
    "espanol": "es",
    "castellano": "es",
    "fr": "fr",
    "fra": "fr",
    "fre": "fr",
    "french": "fr",
    "de": "de",
    "deu": "de",
    "ger": "de",
    "german": "de",
    "deutsch": "de",
    "it": "it",
    "ita": "it",
    "italian": "it",
    "ko": "ko",
    "kor": "ko",
    "korean": "ko",
    "hi": "hi",
    "hin": "hi",
    "hindi": "hi",
    "bn": "bn",
    "ben": "bn",
    "bengali": "bn",
    "bangla": "bn",
    "pa": "pa",
    "pan": "pa",
    "pun": "pa",
    "punjabi": "pa",
    "mr": "mr",
    "mar": "mr",
    "marathi": "mr",
    "gu": "gu",
    "guj": "gu",
    "gujarati": "gu",
    "ta": "ta",
    "tam": "ta",
    "tamil": "ta",
    "te": "te",
    "tel": "te",
    "telugu": "te",
    "kn": "kn",
    "kan": "kn",
    "kannada": "kn",
    "ml": "ml",
    "mal": "ml",
    "malayalam": "ml",
    "th": "th",
    "tha": "th",
    "thai": "th",
    "vi": "vi",
    "vie": "vi",
    "vietnamese": "vi",
    "id": "id",
    "ind": "id",
    "indonesian": "id",
    "tr": "tr",
    "tur": "tr",
    "turkish": "tr",
    "he": "he",
    "heb": "he",
    "hebrew": "he",
    "fa": "fa",
    "fas": "fa",
    "per": "fa",
    "persian": "fa",
    "farsi": "fa",
    "uk": "uk",
    "ukr": "uk",
    "ukrainian": "uk",
    "el": "el",
    "ell": "el",
    "gre": "el",
    "greek": "el",
    "lt": "lt",
    "lit": "lt",
    "lithuanian": "lt",
    "lv": "lv",
    "lav": "lv",
    "latvian": "lv",
    "et": "et",
    "est": "et",
    "estonian": "et",
    "pl": "pl",
    "pol": "pl",
    "polish": "pl",
    "cs": "cs",
    "ces": "cs",
    "cze": "cs",
    "czech": "cs",
    "sk": "sk",
    "slk": "sk",
    "slo": "sk",
    "slovak": "sk",
    "hu": "hu",
    "hun": "hu",
    "hungarian": "hu",
    "ro": "ro",
    "ron": "ro",
    "rum": "ro",
    "romanian": "ro",
    "bg": "bg",
    "bul": "bg",
    "bulgarian": "bg",
    "sr": "sr",
    "srp": "sr",
    "serbian": "sr",
    "hr": "hr",
    "hrv": "hr",
    "croatian": "hr",
    "sl": "sl",
    "slv": "sl",
    "slovenian": "sl",
    "nl": "nl",
    "nld": "nl",
    "dut": "nl",
    "dutch": "nl",
    "da": "da",
    "dan": "da",
    "danish": "da",
    "fi": "fi",
    "fin": "fi",
    "finnish": "fi",
    "sv": "sv",
    "swe": "sv",
    "swedish": "sv",
    "no": "no",
    "nor": "no",
    "norwegian": "no",
    "ms": "ms",
    "msa": "ms",
    "may": "ms",
    "malay": "ms",
    "la": "la",
    "latino": "la",
    "latam": "la",
    "latin american spanish": "la",
    "lat": "la",
}

STREMTHRU_AUDIO_LANGUAGE_IGNORES = frozenset(
    {
        "",
        "und",
        "unknown",
        "unk",
        "mul",
        "multiple",
        "multi",
        "mis",
        "zxx",
    }
)


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


def normalize_stremthru_audio_language(value) -> str | None:
    if not isinstance(value, str):
        return None

    cleaned = value.strip().lower()
    if not cleaned:
        return None

    for separator in ("(", "[", "/", ","):
        cleaned = cleaned.split(separator, 1)[0].strip()

    cleaned = cleaned.replace("_", "-").replace(".", "-")
    if cleaned in STREMTHRU_AUDIO_LANGUAGE_IGNORES:
        return None

    candidates = [cleaned]
    if "-" in cleaned:
        candidates.append(cleaned.split("-", 1)[0].strip())

    for candidate in candidates:
        if candidate in STREMTHRU_AUDIO_LANGUAGE_IGNORES:
            continue
        normalized = STREMTHRU_AUDIO_LANGUAGE_ALIASES.get(candidate)
        if normalized:
            return normalized

    return None


def enrich_metadata_from_stremthru(
    parsed: ParsedData | None, file_data
) -> ParsedData | None:
    if parsed is None or not isinstance(file_data, dict):
        return parsed

    media_info = file_data.get("media_info")
    if not isinstance(media_info, dict):
        return parsed

    audio_tracks = media_info.get("audio")
    if not isinstance(audio_tracks, list):
        return parsed

    languages = list(parsed.languages or [])
    seen_languages = set(languages)
    updated = False

    for track in audio_tracks:
        if not isinstance(track, dict):
            continue

        language = normalize_stremthru_audio_language(track.get("lang"))
        if not language or language in seen_languages:
            continue

        languages.append(language)
        seen_languages.add(language)
        updated = True

    if updated:
        parsed.languages = languages
        ensure_multi_language(parsed)

    return parsed


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
