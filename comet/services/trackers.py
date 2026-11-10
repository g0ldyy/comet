from collections.abc import Iterable
from urllib.parse import urlsplit

import aiohttp

from comet.core.logger import logger

TRACKER_SCHEMES = frozenset(("http", "https", "udp"))

trackers = []


def _normalize_tracker(source: str) -> str | None:
    url = source.strip().removeprefix("tracker:")
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    if parsed.scheme in TRACKER_SCHEMES and parsed.hostname:
        return url
    return None


def normalize_trackers(sources: Iterable[str]) -> list[str]:
    """Return unique announce URLs from torrent sources, without the Stremio
    `tracker:` prefix and without DHT or otherwise unusable entries."""
    return list(dict.fromkeys(filter(None, map(_normalize_tracker, sources))))


async def download_best_trackers():
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://raw.githubusercontent.com/ngosang/trackerslist/master/trackers_best.txt"
            ) as response:
                text = await response.text()

        trackers.clear()
        trackers.extend(line for line in text.split("\n") if line)
        logger.log(
            "COMET",
            f"Generic Trackers: downloaded {len(trackers)} trackers",
        )
    except Exception as e:
        logger.warning(f"Failed to download best trackers: {e}")
