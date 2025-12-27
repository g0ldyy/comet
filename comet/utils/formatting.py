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
    """
    Format release group(s) from a ParsedData object into a single display string.
    
    Parameters:
        data (ParsedData): Object that may have a `group` attribute containing either a string or a list of strings representing release group names.
    
    Returns:
        str: A string of group names joined with " • ", or an empty string if no group information is present.
    """
    group_parts = []

    if hasattr(data, "group") and data.group:
        if isinstance(data.group, list):
            group_parts.extend(data.group)
        else:
            group_parts.append(data.group)

    return " • ".join(group_parts) if group_parts else ""


def get_formatted_components(
    data: ParsedData,
    ttitle: str,
    seeders: int,
    size: int,
    tracker: str,
    result_format: list,
):
    """
    Builds a dictionary of formatted display components for a parsed release.
    
    Parameters:
        data (ParsedData): Parsed release data used to generate video, audio, quality, group, and languages components. `data.languages` is read when present.
        ttitle (str): The release title to format as the "title" component.
        seeders (int): Seeder count included when requested and not None.
        size (int): Size in bytes; converted to a human-readable string for the "size" component.
        tracker (str): Tracker name or URL to include in the "tracker" component.
        result_format (list): List of component keys to include (e.g., "title", "video_info", "audio_info", "quality_info", "release_group", "seeders", "size", "tracker", "languages"). The special value "all" includes every component.
    
    Returns:
        dict: Mapping of component keys to their formatted string values. Possible keys include "title", "video", "audio", "quality", "group", "seeders", "size", "tracker", and "languages".
    """
    has_all = "all" in result_format
    components = {}

    if has_all or "title" in result_format:
        components["title"] = f"📄 {ttitle}"

    if has_all or "video_info" in result_format:
        info = format_video_info(data)
        if info:
            components["video"] = f"📹 {info}"

    if has_all or "audio_info" in result_format:
        info = format_audio_info(data)
        if info:
            components["audio"] = f"🔊 {info}"

    if has_all or "quality_info" in result_format:
        info = format_quality_info(data)
        if info:
            components["quality"] = f"⭐ {info}"

    if has_all or "release_group" in result_format:
        info = format_group_info(data)
        if info:
            components["group"] = f"🏷️ {info}"

    if (has_all or "seeders" in result_format) and seeders is not None:
        components["seeders"] = f"👤 {seeders}"

    if has_all or "size" in result_format:
        components["size"] = f"💾 {format_bytes(size)}"

    if has_all or "tracker" in result_format:
        components["tracker"] = f"🔎 {tracker}"

    if (
        (has_all or "languages" in result_format)
        and hasattr(data, "languages")
        and data.languages
    ):
        formatted_languages = "/".join(
            get_language_emoji(language) for language in data.languages
        )
        components["languages"] = formatted_languages

    return components


def format_title(components: dict):
    """
    Assembles a final multi-line title string from preformatted component fragments.
    
    Accepts a dictionary of named components (for example keys: "title", "video", "audio", "quality", "group", "seeders", "size", "tracker", "languages") and combines them into display lines:
    - The "title" component becomes the first line.
    - "video" and "audio" are joined with " | " on a single line.
    - "quality" and "group" are joined with " | " on a single line.
    - "seeders", "size", and "tracker" are joined with spaces on a single line.
    - "languages" becomes its own line.
    If no known components are present, a sentinel string is returned.
    
    Parameters:
        components (dict): Mapping from component names to their formatted string values.
    
    Returns:
        str: The assembled multi-line title; or "Empty result format configuration" when no components are available.
    """
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
    """
    Builds a ChillLink metadata list from formatted components and cache status.
    
    Parameters:
    	components (dict): Mapping of component keys to their formatted string values; the value for the "title" key is omitted from the result.
    	cached (bool): True if the item is cached (adds "⚡ Instant"), False if not cached (adds "⬇️ Not Cached").
    
    Returns:
    	metadata (list): List of metadata strings beginning with a cache indicator followed by the component values (excluding the title), preserving the order of components.
    """
    metadata = []

    if cached:
        metadata.append("⚡ Instant")
    else:
        metadata.append("⬇️ Not Cached")

    for key, value in components.items():
        if key != "title":
            metadata.append(value)

    return metadata