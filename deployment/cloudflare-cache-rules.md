# Cloudflare Configuration

To optimize performance, configure Cloudflare to offload traffic from your server. We use **Cache Rules** to cache stream results while respecting the application's cache headers.

## 1. Public Streams (Cache Rule)
Cache public stream results. Comet controls the TTL via headers.

*   **Rule Name**: Public Streams
*   **Expression**: `(starts_with(http.request.uri.path, "/stream/"))`
*   **Action**: Eligible for Cache
*   **Edge TTL**: Use cache-control header if present
*   **Browser TTL**: Respect origin
*   **Serve stale content while revalidating**: On

## 2. Private Streams (Cache Rule)
Cache private stream results (with user config in URL). The config is part of the URL, so each user+media combination has a unique cache key. This prevents the same user from hitting the origin repeatedly for the same content.

*   **Rule Name**: Private Streams
*   **Expression**: `(http.request.uri.path contains "/stream/" and not starts_with(http.request.uri.path, "/stream/"))`
*   **Action**: Eligible for Cache
*   **Edge TTL**: Use cache-control header if present
*   **Browser TTL**: Respect origin
*   **Serve stale content while revalidating**: On

## 3. Tiered Cache
Enable **Tiered Cache** in **Caching > Tiered Cache**.
This minimizes requests to your origin by checking other Cloudflare datacenters first.
