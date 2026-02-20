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
- `POST /configure/login`

## Stremio

- `GET /manifest.json`
- `GET /{b64config}/manifest.json`
- `GET /stream/{media_type}/{media_id}.json`
- `GET /{b64config}/stream/{media_type}/{media_id}.json`
- `GET /{b64config}/playback/{hash}/{service_index}/{index}/{season}/{episode}/{torrent_name}`
- `GET /{b64config}/debrid-sync/{service_index}`

Legacy playback route is still present:

- `GET /{b64config}/playback/{hash}/{index}/{season}/{episode}/{torrent_name}`

## ChillLink

- `GET /manifest`
- `GET /{b64config}/manifest`
- `GET /streams`
- `GET /{b64config}/streams`

## Admin

- `GET /admin` (login page)
- `POST /admin/login`
- `POST /admin/logout`
- `GET /admin/dashboard`

Admin API examples:

- `/admin/api/connections`
- `/admin/api/logs`
- `/admin/api/metrics`
- `/admin/api/update-check`
- `/admin/api/background-scraper/*`
- `/admin/api/cometnet/*`

Most admin APIs require valid `admin_session` cookie, except metrics when `PUBLIC_METRICS_API=True`.

## Kodi

- `POST /kodi/generate_setup_code`
- `POST /kodi/associate_manifest`
- `GET /kodi/get_manifest/{code}`

## CometNet (Integrated Endpoint Surface)

- `WS /cometnet/ws`
- `GET /cometnet/health`

CometNet admin operations are exposed through `/admin/api/cometnet/*` via either local integrated backend or relay backend.

## Next

- [Troubleshooting Guide](../troubleshooting.md)
