_SQLITE_VERSION_CACHE: str | None = None
_SQLITE_MIN_VERSION_SUPPORT_CACHE: dict[tuple[int, int, int], bool] = {}


def parse_sqlite_version(version: str | None) -> tuple[int, int, int]:
    if not version:
        return (0, 0, 0)

    parsed = []
    for part in str(version).split(".")[:3]:
        try:
            parsed.append(int(part))
        except ValueError:
            parsed.append(0)
    while len(parsed) < 3:
        parsed.append(0)
    return (parsed[0], parsed[1], parsed[2])


async def get_sqlite_version(database) -> str:
    global _SQLITE_VERSION_CACHE

    if _SQLITE_VERSION_CACHE is not None:
        return _SQLITE_VERSION_CACHE

    row = await database.fetch_one(
        "SELECT sqlite_version() AS sqlite_version",
        force_primary=True,
    )
    if not row:
        _SQLITE_VERSION_CACHE = "unknown"
        return _SQLITE_VERSION_CACHE

    version = row[0] if isinstance(row, tuple) else row["sqlite_version"]
    _SQLITE_VERSION_CACHE = str(version)
    return _SQLITE_VERSION_CACHE


async def sqlite_supports_min_version(
    database, min_version: tuple[int, int, int]
) -> bool:
    cached = _SQLITE_MIN_VERSION_SUPPORT_CACHE.get(min_version)
    if cached is not None:
        return cached

    supported = parse_sqlite_version(await get_sqlite_version(database)) >= min_version
    _SQLITE_MIN_VERSION_SUPPORT_CACHE[min_version] = supported
    return supported


async def sqlite_supports_returning(database) -> bool:
    return await sqlite_supports_min_version(database, (3, 35, 0))
