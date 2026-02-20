# Streaming, Playback, and Debrid Flow

## High-Level Request Flow

1. Client requests manifest (`/manifest.json` or `/{b64config}/manifest.json`).
2. Client requests streams (`/stream/{media_type}/{media_id}.json` or configured route).
3. Comet fetches metadata, reads cache, may scrape, may check debrid availability.
4. Stream list is returned with playback URLs.
5. Playback endpoint resolves/generates download link and either:
- redirects (`302`) to debrid link, or
- proxies the stream when proxy mode is enabled and authorized.

## Stream Endpoint Logic Highlights

`stream.py` behavior includes:

- Media type filter (`movie`/`series` only).
- Config decoding/validation via base64 config.
- Optional digital-release blocking (`DIGITAL_RELEASE_FILTER`).
- Metadata+aliases retrieval and caching.
- Cache-state decision: immediate scrape, background scrape, or wait message.
- Multi-debrid availability checks and per-service cached state.
- Optional debrid account snapshot enrichment (`scrapeDebridAccountTorrents`).
- RTN filtering/ranking with user config.
- Response assembly for:
- debrid streams (with cached/uncached indicators)
- direct torrent streams when enabled

## Playback Endpoint Logic

`playback.py`:

- Reads selected debrid service credentials from config.
- Checks `download_links_cache` first (1-hour freshness window).
- If cache miss, calls debrid provider link generation.
- Stores generated link in `download_links_cache`.
- If proxy mode is active and authorized, streams through `mediaflow-proxy` wrapper.
- Otherwise returns HTTP redirect to provider link.

## Debrid Sync Trigger

`/{b64config}/debrid-sync/{service_index}` triggers account snapshot refresh and returns a status video response.

## Status Video Responses

When provider errors occur, Comet may return mp4 status assets from `comet/assets/status_videos` instead of JSON errors.

Status key normalization is implemented in `comet/utils/status_keys.py`.

## HTTP Caching

When `HTTP_CACHE_ENABLED=True`:

- Manifest and stream responses include ETag and Cache-Control policies.
- Empty stream responses use short cache policy.
- Configure page caching is conditional (disabled for password-protected configure page).

## Next

- [Scrapers, Background Scraper, and DMM](04-scrapers-background-and-dmm.md)
