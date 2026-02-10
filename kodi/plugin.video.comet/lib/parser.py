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
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def parse_stream_info(_name: str, _description: str, behavior_hints: dict):
    stream_info = _DEFAULTS.copy()
    stream_info["size"] = behavior_hints.get("videoSize")
    stream_info["languages"] = []

    kodi_meta = behavior_hints.get(KODI_META_KEY)
    if not kodi_meta:
        return stream_info

    stream_info["width"] = _safe_int(kodi_meta.get("width", 0))
    stream_info["height"] = _safe_int(kodi_meta.get("height", 0))
    for field in _FIELDS:
        if field in {"width", "height"}:
            continue
        stream_info[field] = kodi_meta.get(field, _DEFAULTS[field])

    languages = stream_info["languages"]
    if not isinstance(languages, list):
        stream_info["languages"] = []

    return stream_info
