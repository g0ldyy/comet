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

## Stremio Install Fails with HTTP URL

## Symptom

Stremio rejects add-on installation or does not load streams when using a public `http://` manifest URL.

## Cause

Stremio expects HTTPS for non-local add-on URLs. HTTP is supported only for local desktop usage (`127.0.0.1` / `localhost`).

## Action

- If Comet is remote/public, put it behind a reverse proxy with HTTPS and reinstall from the new URL.
- If Comet is local on the same machine as Stremio Desktop, use `http://127.0.0.1:8000` or `http://localhost:8000`.

## Usenet Connection Test Fails

## Symptom

The configure page marks a discovery source or playback provider as invalid or
temporarily unavailable.

## Cause

Authentication, account-plan, endpoint reachability, local-origin policy, or a
provider-side outage can fail independently.

## Action

- Test the discovery source and playback provider separately.
- Recheck the exact credentials and provider plan. TorBox Usenet requires
  Usenet access on the account.
- For a local indexer or bridge, add its exact HTTP(S) origin, including the
  port, to `USENET_PRIVATE_UPSTREAM_ORIGINS`. For NzbDAV at
  `http://nzbdav:3000/`, use `["http://nzbdav:3000"]`.
- Retry temporary failures; replace credentials for authentication failures.

## Built-in Usenet Engine Does Not Start

## Symptom

Readiness reports the engine unavailable, or Comet exits when
`USENET_ENGINE_REQUIRED=True`.

## Cause

The engine configuration, persistent/runtime paths, resource limits, NNTP
servers, or host-kernel sandbox contract is invalid.

## Action

- Verify `USENET_ENABLED=True` and `USENET_ENGINE_ENABLED=True`.
- On the Docker host, verify Landlock ABI 3 or newer is available and enabled.
- Use the standard Compose data volume and private `/run/comet/usenet` tmpfs,
  or provide equivalent writable paths with correct ownership.
- Validate every `USENET_NATIVE_SERVERS` entry and keep configured connection
  counts within the provider plan.
- Check readiness and category-only startup logs; secrets and raw provider
  responses are intentionally not logged.

For temporary investigation, change `LOG_PROFILE` from `normal` to `verbose`
and recreate the container. Use `debug` only for a short reproduction, return
to `normal`, and review the complete extract before sharing it. A simple
container restart does not reload `.env`.

## Comet Usenet Access Is Rejected

## Cause

The addon token is missing/wrong, or the operator did not offer the requested
server source.

## Action

- Enter the exact `USENET_NATIVE_ACCESS_TOKEN` value in the **Comet Usenet**
  playback provider.
- If the token was left empty, retrieve the `USENET_NATIVE_ACCESS_TOKEN`
  `config.generated_secret` event from the latest container startup logs.
- Configure `USENET_NATIVE_SERVERS` for the instance pool.
- Enable `USENET_NATIVE_ALLOW_USER_SERVERS` only if personal servers are
  intentionally allowed.

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

- Check the Scraping workspace or `/api/v1/admin/scraping/snapshot`.
- Start or resume through the targeted dashboard action.
- Verify queue watermark settings and lock behavior across replicas.
