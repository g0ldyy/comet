def normalize_cache_media_ids(
    primary_id: str,
    cache_media_ids: list[str] | None,
) -> list[str]:
    return list(dict.fromkeys((primary_id, *(cache_media_ids or ()))))
