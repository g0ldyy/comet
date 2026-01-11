# Cloudflare Configuration

To optimize performance, configure Cloudflare to offload traffic from your server. We use **Cache Rules** to cache responses while respecting the application's cache headers.

## 1. Streams (Cache Rule)
Cache all stream results (public and private). Comet controls the TTL via headers.

*   **Rule Name**: Streams
*   **Expression**: `(http.request.uri.path contains "/stream/")`
*   **Action**: Eligible for Cache
*   **Edge TTL**: Use cache-control header if present (first option)
*   **Browser TTL**: Respect origin
*   **Serve stale content while revalidating**: On

## 2. Configure Page (Cache Rule)
Cache the configuration page.

*   **Rule Name**: Configure Page
*   **Expression**: `(http.request.uri.path eq "/configure")`
*   **Action**: Eligible for Cache
*   **Edge TTL**: Use cache-control header if present (first option)
*   **Browser TTL**: Respect origin
*   **Serve stale content while revalidating**: On

## 3. Manifest (Cache Rule)
Cache the add-on manifest.

*   **Rule Name**: Manifest
*   **Expression**: `(http.request.uri.path contains "/manifest.json")`
*   **Action**: Eligible for Cache
*   **Edge TTL**: Use cache-control header if present (first option)
*   **Browser TTL**: Respect origin
*   **Serve stale content while revalidating**: On

## 4. Tiered Cache
Enable **Tiered Cache** in **Caching > Tiered Cache**.
This minimizes requests to your origin by checking other Cloudflare datacenters first.

## 5. Network Optimizations
In **Speed > Protocol**:

*   **HTTP/3 (QUIC)**: On (faster connections, especially on mobile)
*   **0-RTT Connection Resumption**: On (reduces latency for repeat visitors)
