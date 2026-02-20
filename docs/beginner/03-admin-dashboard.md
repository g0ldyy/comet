# Use the Admin Dashboard

The Admin Dashboard is the main operations UI for Comet.

## Access

Open:

- `http://<your-host>:8000/admin`

Login with `ADMIN_DASHBOARD_PASSWORD`.

Session behavior:

- Cookie name: `admin_session`
- TTL from `ADMIN_DASHBOARD_SESSION_TTL` (minimum enforced: 60 seconds)

## Main Tabs

1. **Connections**
- Shows active proxied stream connections.
- Includes per-connection traffic metrics and global session/all-time counters.

2. **Logs**
- Shows captured runtime logs from the in-memory log capture.
- Supports filtering API logs in the UI.

3. **Metrics**
- Torrent/search/cache metrics from database queries.
- Endpoint: `/admin/api/metrics`
- If `PUBLIC_METRICS_API=True`, this endpoint is public.

4. **Background Scraper**
- View status, run history, queue and SLO info.
- Start/stop/pause/resume/requeue-dead from the dashboard.

5. **CometNet**
- Visible for CometNet operations and pool management APIs.
- Requires CometNet backend to be active.

## Update Check

The dashboard calls `/admin/api/update-check`, which compares your current build with the branch head on GitHub.

## Security Notes

- Keep `ADMIN_DASHBOARD_PASSWORD` strong.
- If dashboard is exposed to the internet, put it behind HTTPS and network restrictions.

## Next

- [Set Up Kodi](../../kodi/README.md)
