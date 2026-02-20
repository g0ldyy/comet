# Configure and Install in Stremio

This guide walks through first configuration and add-on installation.

## Step 1: Open the Configure Page

Open:

- `http://<your-host>:8000/configure` (direct local access)
- `https://<your-domain>/configure` (reverse proxy / public access)

If `CONFIGURE_PAGE_PASSWORD` is configured, Comet shows a login form first.

## Step 2: Set Your Streaming Options

The configuration page stores settings inside the generated manifest URL.

Main options for beginners:

- Add one or more debrid services in **Debrid Services**.
- Optionally enable direct torrent links with **Enable Torrent streams**.
- Set quality filters (resolutions, languages, max size, rank threshold).

## Step 3: Install in Stremio

Use one of the built-in buttons:

- **Install**: opens the `stremio://.../manifest.json` URL.
- **Copy Link**: copies the manifest URL using the current page origin.

Manifest URL behavior:

- Default config uses `/manifest.json`.
- Custom config uses `/{b64config}/manifest.json`.
- If API protection is enabled (`CONFIGURE_PAGE_PASSWORD` or `PUBLIC_API_TOKEN`), the prefix `/s/<token>` is inserted automatically.

## Important: Stremio HTTP/HTTPS Rule

Stremio expects HTTPS add-on URLs for non-local addresses.

HTTP works only for local desktop usage with `127.0.0.1` or `localhost`.

Practical impact:

- Remote/VPS/domain deployments should expose Comet through HTTPS on a reverse proxy before installing in Stremio.
- If you run Comet locally on the same machine as Stremio Desktop, `http://127.0.0.1:8000` or `http://localhost:8000` can work.

## Step 4: Verify Streams

After installing in Stremio, open a movie or episode.

Comet serves streams from:

- `/stream/{media_type}/{media_id}.json` (default config)
- `/{b64config}/stream/{media_type}/{media_id}.json` (custom config)

## Basic Troubleshooting

- If you see an obsolete configuration message, re-open `/configure` and reinstall.
- If installation fails with a non-local `http://` URL, switch to `https://` (or use local desktop `127.0.0.1`/`localhost`).
- If you configured only debrid services and no torrent mode, ensure API keys are valid.
- If you configured torrent-only mode and `DISABLE_TORRENT_STREAMS=True`, Comet returns a placeholder stream message by design.

## Next

- [Use the Admin Dashboard](03-admin-dashboard.md)
