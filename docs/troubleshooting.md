# Troubleshooting

## Configuration Issues

## Symptom

Obsolete configuration message in manifest or stream output.

## Cause

Invalid or outdated `b64config` payload.

## Action

Open `/configure` again and reinstall using the newly generated manifest URL.

## Debrid Authentication/Playback Issues

## Symptom

Debrid stream fails or returns status video.

## Cause

Provider auth error, non-premium account, invalid API token, or provider-side error.

## Action

- Recheck debrid API keys.
- Validate selected debrid service in your config.
- Check Comet logs and status video key.

## Stremio Install Fails with HTTP URL

## Symptom

Stremio rejects add-on installation or does not load streams when using a public `http://` manifest URL.

## Cause

Stremio expects HTTPS for non-local add-on URLs. HTTP is supported only for local desktop usage (`127.0.0.1` / `localhost`).

## Action

- If Comet is remote/public, put it behind a reverse proxy with HTTPS and reinstall from the new URL.
- If Comet is local on the same machine as Stremio Desktop, use `http://127.0.0.1:8000` or `http://localhost:8000`.

## Proxy Stream Limit Reached

## Symptom

`PROXY_LIMIT_REACHED` status video.

## Cause

Active connection count for the client IP reached `PROXY_DEBRID_STREAM_MAX_CONNECTIONS`.

## Action

- Close active streams from that IP.
- Increase `PROXY_DEBRID_STREAM_MAX_CONNECTIONS` if intended.

## SQLite Concurrency Problems

## Symptom

Locking/performance issues under load.

## Cause

SQLite backend with multiple workers or heavy background operations.

## Action

Use PostgreSQL for production workloads.

## CometNet Start Failures

## Symptom

CometNet exits at startup with critical log.

## Common Causes

- Integrated mode with `FASTAPI_WORKERS > 1`
- Invalid or missing `COMETNET_ADVERTISE_URL` on public deployments
- Reachability check failure
- Time sync check failure

## Action

- For multi-worker deployments, use relay mode (`COMETNET_RELAY_URL`).
- Set a reachable `wss://` advertise URL.
- Fix reverse-proxy websocket forwarding.
- Sync system clock or adjust CometNet check options intentionally.

## Background Scraper Not Running

## Symptom

No progress in background queue.

## Cause

Not enabled, paused, lock held by another instance, or queue policy blocked discovery.

## Action

- Check `/admin/api/background-scraper/status`.
- Start/resume via dashboard/API.
- Verify queue watermark settings and lock behavior across replicas.
