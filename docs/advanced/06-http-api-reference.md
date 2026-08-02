# HTTP API Reference

## Prefix Model

Stremio-family endpoints are mounted under `STREMIO_API_PREFIX` when API protection is enabled.

Examples:

- without prefix: `/manifest.json`
- with prefix: `/s/<token>/manifest.json`

## General

- `GET /` -> redirect to `/configure`
- `GET /health` -> application health (`{"status":"ok"}`)

## Configuration UI

- `GET /configure`
- `GET /{b64config}/configure`
- `POST /api/v1/auth/configure/login`

## Stremio

- `GET /manifest.json`
- `GET /{b64config}/manifest.json`
- `GET /stream/{media_type}/{media_id}.json`
- `GET /{b64config}/stream/{media_type}/{media_id}.json`
- `GET /{b64config}/playback/{hash}/{service_index}/{index}/{season}/{episode}?torrent_name={torrent_name}&name={name}`
- `GET /{b64config}/debrid-sync/{service_index}`

## Torznab

The complete endpoint is `https://host[/s/token]/torznab/api`. It exposes the
Movies and TV categories and supports text, IMDb, season, and episode searches.
Capabilities can be checked with `GET /torznab/api?t=caps`.

See the [Torznab client integration guide](../integrations/torznab.md) for the
official Prowlarr, Sonarr, Radarr, and Jackett configurations.

## ChillLink

- `GET /manifest`
- `GET /{b64config}/manifest`
- `GET /streams`
- `GET /{b64config}/streams`

## Admin

- `GET /admin` and `GET /admin/*` serve the local admin SPA.
- `POST /api/v1/auth/login`
- `POST /api/v1/auth/logout`

Admin API examples:

- `/api/v1/admin/logs`
- `/api/v1/admin/metrics/*`
- `/api/v1/admin/proxy/*`
- `/api/v1/admin/scraping/*`
- `/api/v1/admin/usenet/*`
- `/api/v1/admin/cometnet/*`
- `/api/v1/admin/system/*`

All admin APIs require a valid `admin_session`; mutations additionally require
the same-origin CSRF token returned by the session API.

## Kodi

- `POST /kodi/generate_setup_code`
- `POST /kodi/associate_manifest`
- `GET /kodi/get_manifest/{code}`

## CometNet (Integrated Endpoint Surface)

- `WS /cometnet/ws`
- `GET /cometnet/health`

CometNet admin operations are exposed through `/api/v1/admin/cometnet/*` via
either the local integrated backend or relay backend.

## Next

- [Troubleshooting Guide](../troubleshooting.md)
