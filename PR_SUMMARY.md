# Branch Summary

- Added replica-aware database routing (`comet/core/db_router.py`, `comet/core/database.py`, `comet/core/models.py`), letting PostgreSQL deployments list `DATABASE_READ_REPLICA_URLS` so reads can go to replicas while writes remain on the primary, plus a force-primary escape hatch inside maintenance queries.
- Reduced startup thrash by gating the heavy cleanup sweep behind `DATABASE_STARTUP_CLEANUP_INTERVAL` and persisting anime mapping data in `anime_mapping_cache` tables so most boots load instantly from the DB instead of redownloading (`comet/core/database.py`, `comet/services/anime.py`, env knobs `ANIME_MAPPING_SOURCE`/`ANIME_MAPPING_REFRESH_INTERVAL`).
- Hardened torrent ingestion and ranking: `TorrentManager` now funnels inserts through an `INSERT ... ON CONFLICT DO UPDATE` helper to quiet duplicate-key races, and ranking/logging paths gained small cleanups (`comet/services/torrent_manager.py`, `comet/services/lock.py`, logging tweaks in `comet/core/log_levels.py`).
- Added an env-toggle to skip Gunicorn preload (`GUNICORN_PRELOAD_APP`) so large deployments can trade startup cost vs. schema-read requirements (`comet/main.py`, `.env-sample`).
- Introduced `DISABLE_TORRENT_STREAMS` plus customizable placeholder metadata so operators can block magnet-only usage and show a friendly Stremio message instead (`comet/api/endpoints/stream.py`, `comet/core/models.py`, `.env-sample`).
- Updated documentation/config samples and changelog to capture the new settings and behavior (`.env-sample`, `CHANGELOG.md`).
