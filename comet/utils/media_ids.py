_MAX_CACHE_MEDIA_IDS = 64
_MAX_CACHE_MEDIA_ID_BYTES = 128


def _is_bounded_media_id(value: object) -> bool:
    if type(value) is not str or not value:
        return False
    try:
        return len(value.encode("utf-8")) <= _MAX_CACHE_MEDIA_ID_BYTES
    except UnicodeEncodeError:
        return False


def normalize_cache_media_ids(
    primary_id: str,
    cache_media_ids: list[str] | None,
) -> list[str]:
    if not _is_bounded_media_id(primary_id):
        raise ValueError("primary cache media ID must be a bounded UTF-8 string")
    if cache_media_ids is not None and type(cache_media_ids) is not list:
        raise TypeError("cache media IDs must be a list or None")
    if not cache_media_ids:
        return [primary_id]

    seen = {primary_id}
    cleaned = [primary_id]
    for media_id in cache_media_ids:
        if (
            not _is_bounded_media_id(media_id)
            or media_id in seen
            or len(cleaned) >= _MAX_CACHE_MEDIA_IDS
        ):
            continue
        seen.add(media_id)
        cleaned.append(media_id)

    return cleaned
