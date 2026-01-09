# Cloudflare Configuration

To optimize performance, configure Cloudflare to offload traffic from your server. We use **Cache Rules** to cache public stream results while respecting the application's cache headers.

## 1. Public Streams (Cache Rule)
Allow Cloudflare to cache public stream results. Comet automatically controls the duration (TTL) and freshness (Stale-While-Revalidate) via headers.

*   **Rule Name**: Public Streams
*   **Expression**: `(starts_with(http.request.uri.path, "/stream/"))`
*   **Action**: Eligible for Cache
*   **Edge TTL**: Use cache-control header if present
*   **Browser TTL**: Respect origin
*   **Serve stale content while revalidating**: On

## 2. Tiered Cache
Enable **Tiered Cache** in **Caching > Tiered Cache**.
This minimizes requests to your origin by checking other Cloudflare datacenters first.

---
*Note: Private streams (URLs containing your configuration) are automatically excluded because they do not start with `/stream/`.*
