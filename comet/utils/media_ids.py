def normalize_cache_media_ids(
    primary_id: str,
    cache_media_ids: list[str] | None,
) -> list[str]:
    if type(primary_id) is not str or not primary_id:
        raise ValueError("primary cache media ID must be a non-empty string")
    if cache_media_ids is not None and type(cache_media_ids) is not list:
        raise TypeError("cache media IDs must be a list or None")
    if not cache_media_ids:
        return [primary_id]

    seen = set()
    cleaned: list[str] = []
    for media_id in cache_media_ids:
        if type(media_id) is not str or not media_id or media_id in seen:
            continue
        seen.add(media_id)
        cleaned.append(media_id)

    if primary_id not in seen:
        cleaned.insert(0, primary_id)

    return cleaned
