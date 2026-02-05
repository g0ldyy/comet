def normalize_cache_media_ids(
    primary_id: str,
    primary_is_kitsu: bool,
    cache_media_ids: list[tuple[str, bool]] | None,
) -> list[tuple[str, bool]]:
    if not cache_media_ids:
        return [(primary_id, primary_is_kitsu)]

    seen = set()
    cleaned: list[tuple[str, bool]] = []
    for media_id, is_kitsu in cache_media_ids:
        if not media_id or media_id in seen:
            continue
        seen.add(media_id)
        cleaned.append((media_id, bool(is_kitsu)))

    if primary_id and primary_id not in seen:
        cleaned.insert(0, (primary_id, primary_is_kitsu))

    return cleaned
