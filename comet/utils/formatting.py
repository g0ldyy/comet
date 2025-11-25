from RTN import ParsedData


def format_bytes(bytes_value):
    if bytes_value is None:
        return "0 B"

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
        info_parts.append(f"💾 {format_bytes(size)}")

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
