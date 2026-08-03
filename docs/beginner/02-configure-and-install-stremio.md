# Configure and Install in Stremio

This guide walks through first configuration and add-on installation.

## Step 1: Open the Configure Page

Open:

- `http://<your-host>:8000/configure` (direct local access)
- `https://<your-domain>/configure` (reverse proxy / public access)

If `CONFIGURE_PAGE_PASSWORD` is configured, Comet shows a login form first.

## Step 2: Set Your Streaming Options

The configuration page stores settings inside the generated manifest URL. New
links use a compressed, self-contained segment; no server-side configuration
record is required. Existing uncompressed installation URLs remain supported
and Comet automatically uses the compact form for playback links it returns.

Main options for beginners:

- Add one or more debrid services in **Debrid Services**.
- Optionally enable direct torrent links with **Enable Torrent streams**.
- Set quality filters (resolutions, languages, and max size).

## Optional: Configure Usenet

The operator must first set `USENET_ENABLED=True`. This exposes the Usenet
section; it does not require the built-in engine.

In the Usenet section:

1. Turn on **Enable Usenet** and decide whether to keep torrent/debrid sources
   too.
2. Add at least one **Discovery source**. This is where Comet searches, such as
   Newznab/NZBHydra2/Prowlarr, Easynews Search, or a compatible Stremio addon.
3. Add at least one **Playback provider**. This is where the selected result is
   read, such as TorBox Usenet, Easynews, NzbDAV, AltMount, StremThru Newz,
   Stremio NNTP, or the Comet built-in engine.
4. Use **Test enabled Usenet connections** before installing. Each entry also
   has its own connection test.

TorBox Usenet requires an account plan with Usenet access.

The generated addon URL contains any credentials entered on this page. Keep it
private, use HTTPS outside localhost, and do not paste it into logs or support
messages.

### Built-in engine

The built-in engine is optional. The operator must additionally configure:

```env
USENET_ENGINE_ENABLED=True
# Optional: set a stable private value of 32 to 256 characters.
USENET_NATIVE_ACCESS_TOKEN=
```

The Docker host needs Landlock ABI 3 or newer, as described in
[Get Started with Docker](01-get-started-docker.md). The standard Compose file
already persists `/app/data` and provides the private runtime directory.
When the token is empty, Comet generates it in the shared database and prints
it as `config.generated_secret` in the startup logs.

The operator chooses one or both server sources:

- Set `USENET_NATIVE_SERVERS` for operator-owned instance NNTP servers.
- Set `USENET_NATIVE_ALLOW_USER_SERVERS=True` to let addon users enter their
  own NNTP servers.

On the configure page, add **Comet Usenet** as a playback provider and enter
the built-in access token with its other provider settings. Comet validates the
token when testing the provider, copying the link, or installing in Stremio.
Keep the default resource limits unless measurements justify changing them.

### Stremio NNTP handoff

Stremio NNTP sends the configured NNTP credentials to a current full Stremio
client and the client reads the media directly. Use implicit TLS whenever
possible. This path does not use Comet's archive or PAR2 repair engine.

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
- If Usenet returns no choices, test the discovery source and playback provider
  separately; both are required for a usable path.
- If built-in access is unavailable, verify the operator enabled the engine,
  configured an access token, and offered the selected NNTP server source.
- If you configured torrent-only mode and `DISABLE_TORRENT_STREAMS=True`, Comet returns a placeholder stream message by design.

## Next

- [Use the Admin Dashboard](03-admin-dashboard.md)
