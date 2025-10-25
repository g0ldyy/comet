import re
import asyncio
import time

from comet.utils.general import (
    size_to_bytes,
    log_scraper_error,
    fetch_with_proxy_fallback,
)


data_pattern = re.compile(
    r"(?:👤 (\d+) )?💾 ([\d.]+ [KMGT]B)(?: ⚙️ (\w+))?", re.IGNORECASE
)

_rate_limiters = {}

class RateLimiter:
    def __init__(self, max_requests: int = 5, time_window: float = 1.0):
        self.max_requests = max_requests
        self.time_window = time_window
        self.requests = []
        self._lock = asyncio.Lock()
    
    async def acquire(self):
        async with self._lock:
            now = time.time()
            self.requests = [req_time for req_time in self.requests if now - req_time < self.time_window]
            
            if len(self.requests) < self.max_requests:
                self.requests.append(now)
                return
            
            oldest_request = min(self.requests)
            sleep_time = self.time_window - (now - oldest_request)
            if sleep_time > 0:
                await asyncio.sleep(sleep_time)
                self.requests.append(time.time())

def get_rate_limiter(url: str) -> RateLimiter:
    if url not in _rate_limiters:
        _rate_limiters[url] = RateLimiter()
    return _rate_limiters[url]


async def get_torrentio(manager, url: str):
    torrents = []
    
    rate_limiter = get_rate_limiter(url)
    await rate_limiter.acquire()
    
    try:
        results = await fetch_with_proxy_fallback(
            f"{url}/stream/{manager.media_type}/{manager.media_id}.json"
        )

        for torrent in results["streams"]:
            title_full = torrent["title"]

            if "\n💾" in title_full:
                title = title_full.split("\n💾")[0].split("\n")[-1]
            else:
                title = title_full.split("\n")[0]

            match = data_pattern.search(title_full)

            seeders = int(match.group(1)) if match.group(1) else None
            size = size_to_bytes(match.group(2))
            tracker = match.group(3) if match.group(3) else "KnightCrawler"

            torrents.append(
                {
                    "title": title,
                    "infoHash": torrent["infoHash"].lower(),
                    "fileIndex": torrent.get("fileIdx", None),
                    "seeders": seeders,
                    "size": size,
                    "tracker": f"Torrentio|{tracker}",
                    "sources": torrent.get("sources", []),
                }
            )
    except Exception as e:
        log_scraper_error("Torrentio", url, manager.media_id, e)
        await asyncio.sleep(2.0)

    await manager.filter_manager(torrents)


