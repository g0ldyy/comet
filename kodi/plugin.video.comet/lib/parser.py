KODI_META_KEY = "cometKodiMetaV1"

_FIELDS = (
    "width",
    "height",
    "language",
    "hdr",
    "codec",
    "resolution",
    "audio",
    "channels",
    "title",
    "videoInfo",
    "audioInfo",
    "qualityInfo",
    "groupInfo",
    "seedersInfo",
    "sizeInfo",
    "trackerInfo",
    "languagesInfo",
)
_DEFAULTS = {field: "" for field in _FIELDS}
_DEFAULTS["width"] = 0
_DEFAULTS["height"] = 0


def _safe_int(value):
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def parse_stream_info(_name: str, _description: str, behavior_hints: dict):
    stream_info = _DEFAULTS.copy()
    if not isinstance(behavior_hints, dict):
        stream_info["size"] = 0
        stream_info["languages"] = []
        return stream_info

    video_size = behavior_hints.get("videoSize")
    stream_info["size"] = (
        video_size if type(video_size) is int and video_size >= 0 else 0
    )
    stream_info["languages"] = []

    kodi_meta = behavior_hints.get(KODI_META_KEY)
    if not isinstance(kodi_meta, dict):
        return stream_info

    stream_info["width"] = _safe_int(kodi_meta.get("width", 0))
    stream_info["height"] = _safe_int(kodi_meta.get("height", 0))
    for field in _FIELDS:
        if field in {"width", "height"}:
            continue
        value = kodi_meta.get(field, _DEFAULTS[field])
        stream_info[field] = value if isinstance(value, str) else _DEFAULTS[field]

    languages = kodi_meta.get("languages")
    stream_info["languages"] = (
        [language for language in languages if isinstance(language, str)]
        if isinstance(languages, list)
        else []
    )

    return stream_info
