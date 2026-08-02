# Use the Admin Dashboard

The Admin Dashboard is the main operations UI for Comet.

## Access

Open:

- `http://<your-host>:8000/admin`

Login with `ADMIN_DASHBOARD_PASSWORD`.

If the variable is omitted or empty, Comet generates a password and emits it
once as `config.generated_secret` in the container startup logs.

Session behavior:

- Cookie name: `admin_session`
- TTL from `ADMIN_DASHBOARD_SESSION_TTL` (minimum enforced: 60 seconds)

## Main Workspaces

- **Overview and Analytics** combine live telemetry with useful torrent,
  search, debrid-cache, scraper, proxy, database, and Usenet aggregates.
- **Logs** stream bounded structured events across all registered
  Comet processes.
- **Proxy, Scraping, Usenet, and CometNet** expose live work, useful history,
  and only targeted actions supported by the owning runtime.
- **Settings** edits shared typed settings with revisions and audit history.
- **System** shows build/update information, readiness, replicas/processes,
  storage, capabilities, and safe retention.

## Update Check

The System workspace performs an explicit update check against the current
branch head on GitHub. It provides update instructions but never updates or
restarts Comet automatically.

## Security Notes

- Keep `ADMIN_DASHBOARD_PASSWORD` strong.
- If dashboard is exposed to the internet, put it behind HTTPS and network restrictions.
- Dashboard APIs live under `/api/v1/admin/*`, require the signed
  `admin_session`, and require same-origin CSRF for mutations.
## Next

- [Set Up Kodi](../../kodi/README.md)
