## 2025-05-15 - [Redundant Database Query in Cache Freshness Check]
**Learning:** In the `stream` endpoint, `TorrentManager` already fetches all cached torrents, including their `updated_at` timestamps. However, `CacheStateManager` was performing a separate `SELECT 1` query to check for the existence of "fresh" torrents based on the same `updated_at` column.
**Action:** Track the `max_updated_at` timestamp during the initial fetch in `TorrentManager` and pass it to `CacheStateManager` to avoid the redundant database hit.

## 2025-05-16 - [Faster ETag Generation with xxHash]
**Learning:** ETag generation was using `hashlib.md5`, which is a cryptographic hash and relatively slow for large JSON payloads. Since ETags only require a unique identifier for content changes and not cryptographic security, `xxhash.xxh64` is a much faster alternative.
**Action:** Replaced `hashlib.md5` with `xxhash.xxh64` in `comet/utils/cache.py`. Benchmark showed ~10x speedup (0.80s vs 0.07s for 100k iterations on a typical payload).
## 2026-06-30 - [Redundant Database Query when Cache is Empty]
**Learning:** If `TorrentManager` returns 0 torrents from the cache, it's logically impossible for any "fresh" torrents to exist. Calling `get_fresh_torrent_count` in this case results in a redundant `SELECT 1` database query.
**Action:** Short-circuit the `fresh_count` to 0 when `torrent_count` is 0 in `CacheStateManager.check_and_decide`.
