import base64

from RTN import ParsedData

from comet.core.models import settings


def normalize_info_hash(info_hash: str) -> str:
    if len(info_hash) == 32:
        try:
            info_hash = base64.b16encode(base64.b32decode(info_hash.upper())).decode(
                "utf-8"
            )
        except Exception:
            pass

    if len(info_hash) == 80:
        try:
            decoded_bytes = bytes.fromhex(info_hash)
            decoded_str = decoded_bytes.decode("ascii")
            if len(decoded_str) == 40:
                int(decoded_str, 16)  # Validate it's hex
                info_hash = decoded_str
        except (ValueError, UnicodeDecodeError):
            pass

    return info_hash


def format_bytes(bytes_value):
    if bytes_value is None:
        return None

    bytes_value = float(bytes_value)

    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if bytes_value < 1024.0:
            return f"{bytes_value:.1f} {unit}"
        bytes_value /= 1024.0
    return f"{bytes_value:.1f} PB"


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


comet_clean_tracker = settings.COMET_CLEAN_TRACKER


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
        if comet_clean_tracker and tracker[:6] == "Comet|":
            components["tracker"] = style["tracker_clean"].format(
                tracker.rsplit("|", 1)[-1]
            )
        else:
            components["tracker"] = style["tracker"].format(tracker)

    if (
        (has_all or "languages" in result_format)
        and hasattr(data, "languages")
        and data.languages
    ):
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
