import base64
import math
from decimal import Decimal

from RTN import ParsedData

from comet.core.models import settings
from comet.utils.languages import LANGUAGE_EMOJIS

_MAX_SIGNED_64 = 2**63 - 1


def normalize_info_hash(info_hash: str) -> str:
    if len(info_hash) == 32:
        try:
            info_hash = base64.b16encode(base64.b32decode(info_hash.upper())).decode(
                "utf-8"
            )
        except (ValueError, UnicodeError):
            pass

    if len(info_hash) == 80:
        try:
            decoded_bytes = bytes.fromhex(info_hash)
            decoded_str = decoded_bytes.decode("ascii")
            if len(decoded_str) == 40:
                int(decoded_str, 16)  # Validate it's hex
                info_hash = decoded_str
        except (ValueError, UnicodeError):
            pass

    return info_hash.lower()


def format_bytes(bytes_value):
    if bytes_value is None:
        return None
    if isinstance(bytes_value, bool) or not isinstance(
        bytes_value, (int, float, Decimal)
    ):
        return None
    bytes_value = float(bytes_value)
    if not math.isfinite(bytes_value) or bytes_value < 0:
        return None

    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if bytes_value < 1024.0:
            return f"{bytes_value:.1f} {unit}"
        bytes_value /= 1024.0
    return f"{bytes_value:.1f} PB"


def size_to_bytes(size_str: str):
    sizes = ["b", "kb", "mb", "gb", "tb"]

    if type(size_str) is not str:
        return None
    parts = size_str.split()
    if len(parts) != 2:
        return None
    value, unit = parts

    try:
        value = float(value)
    except ValueError:
        return None
    unit = unit.lower()

    if unit not in sizes or not math.isfinite(value) or value < 0:
        return None

    multiplier = 1024 ** sizes.index(unit)
    size_bytes = value * multiplier
    if not math.isfinite(size_bytes) or size_bytes > _MAX_SIGNED_64:
        return None
    return int(size_bytes)


def get_language_emoji(language: str):
    return LANGUAGE_EMOJIS.get(language.lower(), language)


def format_video_info(data: ParsedData):
    video_parts = []

    if data.codec:
        video_parts.append(data.codec)
    video_parts.extend(data.hdr)
    if data.bit_depth:
        video_parts.append(data.bit_depth)

    return " • ".join(video_parts) if video_parts else ""


def format_audio_info(data: ParsedData):
    audio_parts = [*data.audio, *data.channels]

    return " • ".join(audio_parts) if audio_parts else ""


def format_quality_info(data: ParsedData):
    quality_parts = []

    if data.quality:
        quality_parts.append(data.quality)
    if data.edition:
        quality_parts.append(data.edition)
    if data.proper:
        quality_parts.append("PROPER")
    if data.repack:
        quality_parts.append("REPACK")
    if data.upscaled:
        quality_parts.append("UPSCALED")
    if data.remastered:
        quality_parts.append("REMASTERED")
    if data.extended:
        quality_parts.append("EXTENDED")

    return " • ".join(quality_parts) if quality_parts else ""


def format_group_info(data: ParsedData):
    return data.group or ""


_STYLE_EMOJI = {
    "title": "📄 {}",
    "video": "📹 {}",
    "audio": "🔊 {}",
    "quality": "⭐ {}",
    "group": "🏷️ {}",
    "seeders": "👤 {}",
    "size": "💾 {}",
    "tracker": "🔎 {}",
    "tracker_clean": "🔎 Comet|{}",
    "languages": None,
}

_STYLE_PLAIN = {
    "title": "{}",
    "video": "{}",
    "audio": "{}",
    "quality": "{}",
    "group": "{}",
    "seeders": "Seeders: {}",
    "size": "Size: {}",
    "tracker": "Source: {}",
    "tracker_clean": "Source: Comet|{}",
    "languages": "Languages: {}",
}


def _get_formatted_components(
    data: ParsedData,
    ttitle: str,
    seeders: int,
    size: int,
    tracker: str,
    result_format: list,
    style: dict,
):
    has_all = "all" in result_format
    components = {}

    if has_all or "title" in result_format:
        components["title"] = style["title"].format(ttitle)

    if has_all or "video_info" in result_format:
        info = format_video_info(data)
        if info:
            components["video"] = style["video"].format(info)

    if has_all or "audio_info" in result_format:
        info = format_audio_info(data)
        if info:
            components["audio"] = style["audio"].format(info)

    if has_all or "quality_info" in result_format:
        info = format_quality_info(data)
        if info:
            components["quality"] = style["quality"].format(info)

    if has_all or "release_group" in result_format:
        info = format_group_info(data)
        if info:
            components["group"] = style["group"].format(info)

    if (has_all or "seeders" in result_format) and seeders is not None:
        components["seeders"] = style["seeders"].format(seeders)

    if (has_all or "size" in result_format) and size is not None:
        components["size"] = style["size"].format(format_bytes(size))

    if (has_all or "tracker" in result_format) and tracker:
        if settings.COMET_CLEAN_TRACKER and tracker[:6] == "Comet|":
            components["tracker"] = style["tracker_clean"].format(
                tracker.rsplit("|", 1)[-1]
            )
        else:
            components["tracker"] = style["tracker"].format(tracker)

    if (has_all or "languages" in result_format) and data.languages:
        lang_fmt = style["languages"]
        if lang_fmt is None:
            components["languages"] = "/".join(
                get_language_emoji(language) for language in data.languages
            )
        else:
            components["languages"] = lang_fmt.format("/".join(data.languages))

    return components


def get_formatted_components(
    data: ParsedData,
    ttitle: str,
    seeders: int,
    size: int,
    tracker: str,
    result_format: list,
):
    return _get_formatted_components(
        data, ttitle, seeders, size, tracker, result_format, _STYLE_EMOJI
    )


def get_formatted_components_plain(
    data: ParsedData,
    ttitle: str,
    seeders: int,
    size: int,
    tracker: str,
    result_format: list,
):
    return _get_formatted_components(
        data, ttitle, seeders, size, tracker, result_format, _STYLE_PLAIN
    )


def format_title(components: dict):
    lines = []

    if "title" in components:
        lines.append(components["title"])

    video_audio = [components[k] for k in ["video", "audio"] if k in components]
    if video_audio:
        lines.append(" | ".join(video_audio))

    quality_group = [components[k] for k in ["quality", "group"] if k in components]
    if quality_group:
        lines.append(" | ".join(quality_group))

    info = [components[k] for k in ["seeders", "size", "tracker"] if k in components]
    if info:
        lines.append(" ".join(info))

    if "languages" in components:
        lines.append(components["languages"])

    if not lines:
        return "Empty result format configuration"

    return "\n".join(lines)


def format_chilllink(components: dict, cached: bool):
    metadata = []

    if cached:
        metadata.append("⚡ Instant")
    else:
        metadata.append("⬇️ Not Cached")

    for key, value in components.items():
        if key != "title":
            metadata.append(value)

    return metadata
