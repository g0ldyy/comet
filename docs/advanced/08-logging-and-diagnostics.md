# Logging and Diagnostics

Comet writes one bounded structured event per line to standard error. Container
logs remain the canonical operator interface; Comet does not write an
application log file. The admin dashboard also exposes a bounded live view of
the worker serving the dashboard request.

## Profiles

| `LOG_PROFILE` | Content |
|---|---|
| `quiet` | Warnings, errors, and critical failures only |
| `normal` | `quiet` plus rare lifecycle, transitions, recovery, and operation summaries |
| `verbose` | `normal` plus HTTP summaries outside probes and aggregated provider phases |
| `debug` | `verbose` plus bounded internal decisions and safe stack frames |

The default is `normal`. Keep it for normal operation. Every profile includes
the exception type and its bounded message on failure events. `debug`
additionally includes compact source frames.

## Format

`LOG_FORMAT=pretty` is the default human-readable form:

```text
2026-07-29 12:34:56 | 🔎 SCRAPER | INFO | Media search completed | content=tt1234567 candidates=12 duration=184ms [search.completed]
```

`LOG_FORMAT=json` renders the same information as a flat JSON object:

```json
{"timestamp":"2026-07-29 12:34:56","level":"INFO","event":"search.completed","message":"Media search completed","process_role":"web_worker","pid":123,"request_id":"4e8fd6b20d4c4c1a9e18fbd8094825c7","outcome":"ok","candidate_count":12,"duration_ms":184}
```

Each event name is stable. Search or alert on names such as
`search.completed`, `discovery.provider.completed`, `playback.completed`,
`stream.completed`, and `usenet.engine.completed`, not on the human message.

## Reading Logs

```bash
docker compose logs -f --tail=200 comet
docker logs -f --tail=200 comet
```

## Diagnostic Workflow

1. Keep `LOG_PROFILE=normal`.
2. Switch temporarily to `verbose` for an aggregated functional trace.
3. Recreate the service after changing `.env`:

   ```bash
   docker compose up -d --force-recreate comet
   ```

   A plain `restart` does not reload environment variables.
4. Use `debug` only for a short, controlled reproduction.
5. Return to `normal` and recreate the service again.

Before sharing logs, review the entire extract.

## Secrets

Configured secrets are never echoed. When Comet creates an operator secret, it
emits a `config.generated_secret` event at startup so the self-hoster can
actually use it. This applies to the generated admin password and, when
relevant, the debrid proxy password, CometNet API key, public API token, Usenet
access token, or Usenet capability secret. Bootstrap-generated values are kept
in the shared database and remain stable across restarts and replicas.

For stable deployments, set the corresponding environment variable explicitly
or use the documented secret file where one exists. Treat startup logs as
sensitive whenever they contain a `config.generated_secret` event.
