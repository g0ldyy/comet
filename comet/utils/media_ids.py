def normalize_cache_media_ids(
    primary_id: str,
    cache_media_ids: list[str] | None,
) -> list[str]:
    if not cache_media_ids:
        return [primary_id]

    seen = set()
    cleaned: list[str] = []
    for media_id in cache_media_ids:
        if not media_id or media_id in seen:
            continue
        seen.add(media_id)
        cleaned.append(media_id)

    if primary_id and primary_id not in seen:
        cleaned.insert(0, primary_id)

    return cleaned
