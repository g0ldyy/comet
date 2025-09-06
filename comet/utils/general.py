import base64
import orjson

from RTN import ParsedData
from fastapi import Request
from curl_cffi import AsyncSession

from comet.utils.models import (
    ConfigModel,
    default_config,
    settings,
    rtn_settings_default,
    rtn_ranking_default,
)
from comet.utils.logger import logger


def config_check(b64config: str):
    try:
        config = orjson.loads(base64.b64decode(b64config).decode())

        if "indexers" in config:
            return False

        validated_config = ConfigModel(**config)
        validated_config = validated_config.model_dump()

        for key in list(validated_config["options"].keys()):
            if key not in [
                "remove_ranks_under",
                "allow_english_in_languages",
                "remove_unknown_languages",
            ]:
                validated_config["options"].pop(key)

        validated_config["options"]["remove_all_trash"] = validated_config[
            "removeTrash"
        ]

        rtn_settings = rtn_settings_default.model_copy(
            update={
                "resolutions": rtn_settings_default.resolutions.model_copy(
                    update=validated_config["resolutions"]
                ),
                "options": rtn_settings_default.options.model_copy(
                    update=validated_config["options"]
                ),
                "languages": rtn_settings_default.languages.model_copy(
                    update=validated_config["languages"]
                ),
            }
        )

        validated_config["rtnSettings"] = rtn_settings
        validated_config["rtnRanking"] = rtn_ranking_default

        if (
            settings.PROXY_DEBRID_STREAM
            and settings.PROXY_DEBRID_STREAM_PASSWORD
            == validated_config["debridStreamProxyPassword"]
            and validated_config["debridApiKey"] == ""
        ):
            validated_config["debridService"] = (
                settings.PROXY_DEBRID_STREAM_DEBRID_DEFAULT_SERVICE
            )
            validated_config["debridApiKey"] = (
                settings.PROXY_DEBRID_STREAM_DEBRID_DEFAULT_APIKEY
            )

        return validated_config
    except Exception:
        return default_config  # if it doesn't pass, return default config


def bytes_to_size(bytes: int):
    sizes = ["Bytes", "KB", "MB", "GB", "TB"]
    if bytes == 0:
        return "0 Byte"

    i = 0
    while bytes >= 1024 and i < len(sizes) - 1:
        bytes /= 1024
        i += 1

    return f"{round(bytes, 2)} {sizes[i]}"


def size_to_bytes(size_str: str):
    sizes = ["b", "kb", "mb", "gb", "tb"]

    value, unit = size_str.split()

    value = float(value)
    unit = unit.lower()

    if unit not in sizes:
        return None

    multiplier = 1024 ** sizes.index(unit)
    return int(value * multiplier)


languages_emojis = {
    "multi": "🌎",  # Dubbed
    "en": "🇬🇧",  # English
    "ja": "🇯🇵",  # Japanese
    "zh": "🇨🇳",  # Chinese
    "ru": "🇷🇺",  # Russian
    "ar": "🇸🇦",  # Arabic
    "pt": "🇵🇹",  # Portuguese
    "es": "🇪🇸",  # Spanish
    "fr": "🇫🇷",  # French
    "de": "🇩🇪",  # German
    "it": "🇮🇹",  # Italian
    "ko": "🇰🇷",  # Korean
    "hi": "🇮🇳",  # Hindi
    "bn": "🇧🇩",  # Bengali
    "pa": "🇵🇰",  # Punjabi
    "mr": "🇮🇳",  # Marathi
    "gu": "🇮🇳",  # Gujarati
    "ta": "🇮🇳",  # Tamil
    "te": "🇮🇳",  # Telugu
    "kn": "🇮🇳",  # Kannada
    "ml": "🇮🇳",  # Malayalam
    "th": "🇹🇭",  # Thai
    "vi": "🇻🇳",  # Vietnamese
    "id": "🇮🇩",  # Indonesian
    "tr": "🇹🇷",  # Turkish
    "he": "🇮🇱",  # Hebrew
    "fa": "🇮🇷",  # Persian
    "uk": "🇺🇦",  # Ukrainian
    "el": "🇬🇷",  # Greek
    "lt": "🇱🇹",  # Lithuanian
    "lv": "🇱🇻",  # Latvian
    "et": "🇪🇪",  # Estonian
    "pl": "🇵🇱",  # Polish
    "cs": "🇨🇿",  # Czech
    "sk": "🇸🇰",  # Slovak
    "hu": "🇭🇺",  # Hungarian
    "ro": "🇷🇴",  # Romanian
    "bg": "🇧🇬",  # Bulgarian
    "sr": "🇷🇸",  # Serbian
    "hr": "🇭🇷",  # Croatian
    "sl": "🇸🇮",  # Slovenian
    "nl": "🇳🇱",  # Dutch
    "da": "🇩🇰",  # Danish
    "fi": "🇫🇮",  # Finnish
    "sv": "🇸🇪",  # Swedish
    "no": "🇳🇴",  # Norwegian
    "ms": "🇲🇾",  # Malay
    "la": "💃🏻",  # Latino
}


def get_language_emoji(language: str):
    language_formatted = language.lower()
    return (
        languages_emojis[language_formatted]
        if language_formatted in languages_emojis
        else language
    )


def format_video_info(data: ParsedData):
    video_parts = []

    if hasattr(data, "codec") and data.codec:
        if isinstance(data.codec, list):
            video_parts.extend(data.codec)
        else:
            video_parts.append(data.codec)
    if hasattr(data, "hdr") and data.hdr:
        video_parts.append(data.hdr) if isinstance(
            data.hdr, str
        ) else video_parts.extend(data.hdr)

    if hasattr(data, "bitDepth") and data.bitDepth:
        if isinstance(data.bitDepth, list):
            video_parts.extend([f"{bd}bit" for bd in data.bitDepth])
        else:
            if data.bitDepth.endswith("bit"):
                video_parts.append(data.bitDepth)
            else:
                video_parts.append(f"{data.bitDepth}bit")
    elif hasattr(data, "bit_depth") and data.bit_depth:
        if isinstance(data.bit_depth, list):
            video_parts.extend([f"{bd}bit" for bd in data.bit_depth])
        else:
            if data.bit_depth.endswith("bit"):
                video_parts.append(data.bit_depth)
            else:
                video_parts.append(f"{data.bit_depth}bit")

    return " • ".join(video_parts) if video_parts else ""


def format_audio_info(data: ParsedData):
    audio_parts = []

    if hasattr(data, "audio") and data.audio:
        if isinstance(data.audio, list):
            audio_parts.extend(data.audio)
        else:
            audio_parts.append(data.audio)
    if hasattr(data, "channels") and data.channels:
        if isinstance(data.channels, list):
            audio_parts.extend(data.channels)
        else:
            audio_parts.append(data.channels)

    return " • ".join(audio_parts) if audio_parts else ""


def format_quality_info(data: ParsedData):
    quality_parts = []

    if hasattr(data, "quality") and data.quality:
        if isinstance(data.quality, list):
            quality_parts.extend(data.quality)
        else:
            quality_parts.append(data.quality)
    if hasattr(data, "remux") and data.remux:
        quality_parts.append("REMUX")
    if hasattr(data, "proper") and data.proper:
        quality_parts.append("PROPER")
    if hasattr(data, "repack") and data.repack:
        quality_parts.append("REPACK")
    if hasattr(data, "upscaled") and data.upscaled:
        quality_parts.append("UPSCALED")
    if hasattr(data, "remastered") and data.remastered:
        quality_parts.append("REMASTERED")
    if hasattr(data, "directorsCut") and data.directorsCut:
        quality_parts.append("DIRECTOR'S CUT")
    elif hasattr(data, "directors_cut") and data.directors_cut:
        quality_parts.append("DIRECTOR'S CUT")
    if hasattr(data, "extended") and data.extended:
        quality_parts.append("EXTENDED")

    return " • ".join(quality_parts) if quality_parts else ""


def format_group_info(data: ParsedData):
    group_parts = []

    if hasattr(data, "group") and data.group:
        if isinstance(data.group, list):
            group_parts.extend(data.group)
        else:
            group_parts.append(data.group)

    return " • ".join(group_parts) if group_parts else ""


def format_title(
    data: ParsedData,
    ttitle: str,
    seeders: int,
    size: int,
    tracker: str,
    result_format: list,
):
    has_all = "all" in result_format
    lines = []

    show_title = has_all or "title" in result_format
    if show_title:
        lines.append(f"📄 {ttitle}")

    show_video = has_all or "video_info" in result_format
    show_audio = has_all or "audio_info" in result_format
    show_quality = has_all or "quality_info" in result_format
    show_group = has_all or "release_group" in result_format

    video_audio_parts = []

    if show_video:
        video_info = format_video_info(data)
        if video_info:
            video_audio_parts.append(f"📹 {video_info}")

    if show_audio:
        audio_info = format_audio_info(data)
        if audio_info:
            video_audio_parts.append(f"🔊 {audio_info}")

    if video_audio_parts:
        lines.append(" | ".join(video_audio_parts))

    quality_parts = []

    if show_quality:
        quality_info = format_quality_info(data)
        if quality_info:
            quality_parts.append(f"⭐ {quality_info}")

    if show_group:
        groups = format_group_info(data)
        if groups:
            quality_parts.append(f"🏷️ {groups}")

    if quality_parts:
        lines.append(" | ".join(quality_parts))

    show_seeders = has_all or "seeders" in result_format
    show_size = has_all or "size" in result_format
    show_tracker = has_all or "tracker" in result_format

    info_parts = []

    if show_seeders and seeders is not None:
        info_parts.append(f"👤 {seeders}")

    if show_size:
        info_parts.append(f"💾 {bytes_to_size(size)}")

    if show_tracker:
        info_parts.append(f"🔎 {tracker}")

    if info_parts:
        lines.append(" ".join(info_parts))

    show_languages = has_all or "languages" in result_format
    if show_languages:
        if hasattr(data, "languages") and data.languages:
            formatted_languages = "/".join(
                get_language_emoji(language) for language in data.languages
            )
            lines.append(f"{formatted_languages}")

    if not lines:
        return "Empty result format configuration"

    return "\n".join(lines)


def get_client_ip(request: Request):
    return (
        request.headers["cf-connecting-ip"]
        if "cf-connecting-ip" in request.headers
        else request.client.host
    )


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


def parse_media_id(media_type: str, media_id: str):
    if "kitsu" in media_id:
        info = media_id.split(":")

        if len(info) > 2:
            return info[1], 1, int(info[2])
        else:
            return info[1], 1, None

    if media_type == "series":
        info = media_id.split(":")
        return info[0], int(info[1]), int(info[2])

    return media_id, None, None


def get_proxies():
    if settings.DEBRID_PROXY_URL:
        return {
            "http": settings.DEBRID_PROXY_URL,
            "https": settings.DEBRID_PROXY_URL,
        }
    return None


async def fetch_with_proxy_fallback(
    url: str, headers: dict = None, params: dict = None, timeout: int = 10
):
    async with AsyncSession(impersonate="chrome") as session:
        try:
            response = await session.get(
                url, headers=headers, params=params, timeout=timeout
            )
            return response.json()
        except Exception as first_error:
            proxies = get_proxies()
            if proxies:
                try:
                    response = await session.get(
                        url, headers=headers, proxies=proxies, params=params, timeout=timeout
                    )
                    return response.json()
                except Exception as second_error:
                    raise second_error
            else:
                raise first_error


def log_scraper_error(
    scraper_name: str, scraper_url: str, media_id: str, error: Exception
):
    api_password_missing = ""
    if "MediaFusion" in scraper_name:
        api_password_missing = " or your API password could be wrong"

    logger.warning(
        f"Exception while getting torrents for {media_id} with {scraper_name} ({scraper_url}), you are most likely being ratelimited{api_password_missing}: {error}"
    )


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
